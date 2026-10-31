"""Video batch export controller — wires the panel to the queue + runner.

The controller is the glue layer between the UI (settings panel + batch
panel) and the core (ExportQueue + VideoJobRunner). It owns the queue
and the runner, translates panel signals into queue operations, and
pushes queue state changes back into the panels.

Threading / ownership
=====================

The queue is a ``QObject`` that lives on the main thread; its
``_process_next`` runs synchronously on the main thread and calls the
runner, which blocks on a nested event loop while the actual export
happens on the worker's ``QThread``. This means:

* UI stays responsive during export (the nested event loop delivers
  signals).
* There is exactly one runner instance per controller (serial queue).
* Cancellation flows: panel → controller → runner.request_cancel() →
  exporter.cancel_export() → checked between frames.

Context resolution
==================

The controller installs a :class:`JobContextProvider` on the runner that
resolves a job's ``timeline_id`` to the live ``EditProject`` /
``TimelineSequence``. The provider is updated whenever the active project
changes (the main window calls :meth:`set_active_project`).
"""

from __future__ import annotations

import logging
import os
from typing import Any, List, Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from src.features.deliver.core.export_queue import ExportQueue, JobStatus  # noqa: F401 — JobStatus used in restore_from_disk
# efr-small-s2 曾把 VideoJobStatus 误判为未用导入删除——restore_from_disk
# 的 NEEDS_RELINK 翻转真实在用（wd8g43 两个用例的 NameError 即其回归）。
from src.features.deliver.core.video_export_job import VideoExportJob, VideoJobStatus
from src.features.deliver.core.video_job_runner import (
    JobContext,
    VideoJobRunner,
)

logger = logging.getLogger(__name__)

__all__ = ["VideoBatchExportController"]

#: N12：测量 worker 保活注册表——QThread 在跑期间持 Python 强引用，
#: 防止控制器/测试对象 GC 时销毁运行中的线程（Qt 致命 abort）。
#: 线程 finished 后自摘除并 deleteLater。
_qc_worker_keepalive: set = set()


class _QcMeasureWorker(QThread):
    """N12 · 逐版本 LUFS 测量的后台执行体（主线程不再同步解码）。

    测量逐文件完整解码（ffprobe/PyAV），随产物规模线性——必须在
    worker 线程执行；失败逐文件记账（entry.input=None + error），
    整体不可用时返回 error 报告（未测≠通过）。cancel 为协作标记，
    由控制器在新批到达时作废旧代结果（代次拒收在结果槽）。
    """

    finished_with_result = pyqtSignal(dict)

    def __init__(self, output_paths: List[str], parent=None) -> None:
        super().__init__(parent)
        self._paths = list(output_paths)
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:  # pragma: no cover — 线程体
        try:
            from src.features.edit.ai_editing.delivery_qc_batch import (
                measure_version_lufs,
            )
            report = {
                "lufs": measure_version_lufs(self._paths),
                "subtitle": {},
                "cancelled": self._cancelled,
            }
        except Exception as exc:  # noqa: BLE001 — QC 不可用不报成功 QC
            logger.debug("multi-version QC unavailable on drain: %s", exc)
            report = {
                "lufs": {path: {"input": None, "error": f"QC 不可用：{exc}"}
                         for path in self._paths},
                "subtitle": {},
            }
        self.finished_with_result.emit(report)


class VideoBatchExportController(QObject):
    """Coordinate the video batch queue between UI and core.

    The controller is intentionally Qt-only: it does not import the UI
    panels. The workspace creates the panels and connects them to the
    controller's signals / methods. This keeps the controller testable
    without instantiating any QWidget.

    Signals (state changes the UI reflects):
        jobs_changed(): the job list changed structurally (added / removed /
            status transition). The UI should do a full refresh.
        job_progress(str): a single job's progress updated. The UI may do
            a row-only patch.
        queue_running_changed(bool, bool): (running, paused) — the toolbar
            state.
        preflight_result(str, object): (job_id, PreflightResult) emitted
            when an add was attempted with blocking issues. The UI can
            show the detail.
    """

    jobs_changed = pyqtSignal()
    job_progress = pyqtSignal(str)
    queue_running_changed = pyqtSignal(bool, bool)
    preflight_result = pyqtSignal(str, object)
    job_finished = pyqtSignal(str, bool)  # job_id, success
    # M21.2c 接线：批队列排空后的逐版本 QC（{lufs, subtitle}——LUFS 逐
    # 产物实测；subtitle 段未测时为空 dict 不冒充）。宿主据此回填
    # _last_multi_version_qc 供多版本 QC 面板呈现。
    multi_version_qc_ready = pyqtSignal(dict)
    # N12：QC 实测失败（探测失败/未测≠通过）显式通知——宿主可据此在
    # UI/交付侧呈现，不再"失败即静默成功"。
    multi_version_qc_failed = pyqtSignal(list)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)

        self._runner = VideoJobRunner(parent=self)
        self._runner.set_context_provider(self._resolve_context)
        self._queue = ExportQueue(runner=self._runner.run_job, parent=self)

        # Active project / sequence map for context resolution.
        self._active_project: Any = None
        self._active_project_id: str = ""
        self._active_project_revision: int = 0
        self._sequences_by_id: dict[str, Any] = {}
        self._all_sequences: List[Any] = []
        # M21.2c 接线：最近一轮排空后的逐版本 QC 报告（{lufs, subtitle}）
        self.last_multi_version_qc: dict = {}
        # N12：批内跟踪 + 异步测量代次（旧结果拒收）
        self._qc_batch_ids: set = set()
        self._qc_generation = 0
        self._qc_worker: Optional["_QcMeasureWorker"] = None

        # Wire queue signals → controller signals (so the UI only needs to
        # listen to the controller).
        self._queue.job_added.connect(self._on_queue_struct_change)
        self._queue.job_removed.connect(self._on_queue_struct_change)
        self._queue.job_started.connect(self._on_queue_struct_change)
        self._queue.job_completed.connect(self._on_job_completed)
        self._queue.job_progress.connect(self._on_queue_progress)
        self._queue.queue_drained.connect(self._on_queue_drained)
        # AUD-12011: if the runner's nested event loop exits without any
        # worker result (typically application shutdown), stop scheduling
        # instead of failing and re-spawning a worker for every remaining job.
        self._runner.aborted.connect(self._on_runner_aborted)

    # =========================================================================
    # Public API (called by the workspace)
    # =========================================================================

    @property
    def queue(self) -> ExportQueue:
        return self._queue

    @property
    def runner(self) -> VideoJobRunner:
        return self._runner

    def set_active_project(
        self,
        project: Any,
        project_id: str = "",
        project_revision: int = 0,
        sequences: Optional[List[Any]] = None,
    ) -> None:
        """Update the live project + sequence list used for context resolution."""
        self._active_project = project
        self._active_project_id = project_id
        self._active_project_revision = project_revision
        # U03（第六轮审计）：``project.timelines`` 的值是轻量
        # TimelineSequenceRef；把它当序列交给导出器会在 ``audio_tracks``
        # 上 AttributeError。这里只登记完整序列；Ref-only 时间线待宿主用
        # ``register_sequence``/``set_timeline_sequences`` promote 后可用。
        self._all_sequences = [
            seq for seq in (sequences or []) if self._is_full_sequence(seq)
        ]
        self._sequences_by_id = {}
        for seq in self._all_sequences:
            self._sequences_by_id[self._sequence_id(seq)] = seq

    @staticmethod
    def _is_full_sequence(candidate: Any) -> bool:
        """True for full TimelineSequence-like objects (not TimelineSequenceRef)."""
        return candidate is not None and (
            hasattr(candidate, "video_tracks") or hasattr(candidate, "audio_tracks")
        )

    @staticmethod
    def _sequence_id(sequence: Any) -> str:
        """Return the stable ID used by jobs and the live sequence cache."""
        sid = (
            getattr(sequence, "timeline_id", None)
            or getattr(sequence, "sequence_id", None)
        )
        return str(sid if sid is not None else id(sequence))

    def register_sequence(self, sequence: Any) -> None:
        """Register or replace one live sequence without resetting the project.

        Deliver calls this when the shared Program viewer switches timelines
        or when the live Edit timeline emits an updated full sequence. The
        project-level map may only contain lightweight refs, so the full
        sequence must be promoted here for queue context resolution.
        """
        if sequence is None or not self._is_full_sequence(sequence):
            # 轻量 Ref 不是可导出序列（U03）；拒绝登记，避免队列上下文
            # 把 Ref 冒充序列交给导出器。
            return
        sequence_id = self._sequence_id(sequence)
        self._sequences_by_id[sequence_id] = sequence

        for index, existing in enumerate(self._all_sequences):
            if self._sequence_id(existing) == sequence_id:
                self._all_sequences[index] = sequence
                break
        else:
            self._all_sequences.append(sequence)

        self._relink_jobs_for_sequence(sequence_id)

    def _relink_jobs_for_sequence(self, sequence_id: str) -> None:
        """R02（第七轮审计）：序列登记后重评估 NEEDS_RELINK 任务。

        ``set_project`` 先于宿主 ``set_timeline_sequences`` promote 完整
        序列执行 ``restore_from_disk``，此时 map 里只有被过滤的轻量
        Ref——存在的目标被误标失联。目标一旦可解析即恢复 pending；
        真正离线的目标（map 始终无此 id）保持失联，不会被误恢复。
        """
        relinked = False
        for job in self._queue.jobs:
            # legacy ExportJob 的 timeline_id 在 config["video_job"] 快照里；
            # 更旧的扁平 config（pre-video-batch 队列）直接存顶层
            # config["timeline_id"]——两条持久化格式都要能重链。
            job_timeline_id = ""
            video_job = getattr(job.config, "get", lambda *_: None)("video_job")
            if isinstance(video_job, dict):
                job_timeline_id = str(video_job.get("timeline_id") or "")
            if not job_timeline_id:
                job_timeline_id = str(
                    getattr(job.config, "get", lambda *_: "")(
                        "timeline_id") or "")
            if job_timeline_id != sequence_id:
                continue
            if job.status != JobStatus.NEEDS_RELINK:
                continue
            job.status = JobStatus.PENDING
            job.error = ""
            relinked = True
        if relinked:
            logger.info(
                "Timeline %s registered; relinked persisted jobs to pending",
                sequence_id,
            )
            self.jobs_changed.emit()

    def add_jobs(self, jobs: List[VideoExportJob]) -> None:
        """Enqueue one or more jobs (already preflighted by the panel)."""
        # N12：批内跟踪——本轮入队的 job 才属于下一份 QC 报告（排空后
        # 测量不混历史 DONE）。
        for job in jobs:
            try:
                legacy = job.to_legacy_job()
                self._queue.enqueue(legacy)
                self._qc_batch_ids.add(str(job.job_id))
            except ValueError as e:
                logger.warning("Cannot enqueue job %s: %s", job.job_id, e)
        # _on_queue_struct_change fires per enqueue; emit a final summary too.

    # =========================================================================
    # N06 · 多版本派生计划的批次入口（预检 + 原子整批入队）
    # =========================================================================

    def enqueue_version_plan(self, plan, base_config: dict,
                             project=None, timeline=None) -> dict:
        """``compile_version_jobs`` 的生产入口（N06）。

        预检（timeline 存在/素材在线/输出路径不与队列冲突）+ 编译 +
        **原子整批入队**：编译结果 ``ok=False``（任一版本拒绝）时整批
        零入队（审计：混入未知画幅/路径冲突时旧路径可返回部分 jobs，
        无上层阻断——半套交付由此杜绝）；语言→字幕轨身份从时间线
        字幕轨 ``caption_metadata.language`` 解析（无对应轨且策略非
        off → 编译器拒绝该版本）。

        Returns:
            ``{"ok": bool, "enqueued": [job_id…], "refusals": [str…]}``
        """
        from src.features.deliver.core.version_compiler import (
            compile_version_jobs,
        )
        # 预检：时间线在册
        timeline_exists = timeline is not None and (
            self._sequence_id(timeline) in self._sequences_by_id
            if getattr(self, "_sequences_by_id", None) else timeline is not None)
        # 预检：素材在线（缺文件即离线）
        all_online = True
        if project is not None and timeline is not None:
            assets = getattr(project, "assets", None) or {}
            for track in (getattr(timeline, "video_tracks", ()) or ()):
                for clip in (getattr(track, "clips", ()) or ()):
                    asset = assets.get(getattr(clip, "asset_id", ""))
                    path = getattr(asset, "path", "") if asset else ""
                    if path and not os.path.exists(str(path)):
                        all_online = False
                        break
                if not all_online:
                    break
        existing = [str(getattr(job, "output_path", "") or "")
                    for job in self._queue.jobs]

        def resolve_track_id(language: str):
            if timeline is None or not language:
                return None
            wanted = str(language).lower()
            for track in (getattr(timeline, "subtitle_tracks", ()) or ()):
                meta = getattr(track, "caption_metadata", None)
                track_lang = str(getattr(meta, "language", "") or "").lower()
                if track_lang == wanted or track_lang.startswith(
                        wanted.split("-")[0] + "-"):
                    return getattr(track, "track_id", None)
            return None

        result = compile_version_jobs(
            plan, base_config,
            source_project_revision=int(
                getattr(project, "revision", 0) or 0) if project else 0,
            timeline_exists=timeline_exists,
            all_assets_online=all_online,
            existing_output_paths=existing,
            resolve_track_id=resolve_track_id,
        )
        if not result.ok:
            # 原子性：任一拒绝 → 整批零入队（不产半套）
            return {"ok": False, "enqueued": [],
                    "refusals": list(result.refusals)}

        jobs: List[VideoExportJob] = []
        for compiled in result.jobs:
            try:
                jobs.append(VideoExportJob.from_flat_config(
                    dict(compiled.config)))
            except Exception as exc:  # noqa: BLE001 — 快照化失败=整批拒绝
                return {"ok": False, "enqueued": [],
                        "refusals": [f"版本 {compiled.linked_version_id} "
                                     f"job 快照化失败：{exc}"]}
        self.add_jobs(jobs)
        return {"ok": True,
                "enqueued": [str(j.job_id) for j in jobs],
                "refusals": []}

    def start_queue(self) -> None:
        if self._queue.is_running:
            return
        self._runner.request_resume()
        self._queue.start()
        self._emit_running_state()

    def pause_queue(self) -> None:
        # Pause the active frame loop as well as stopping the scheduler from
        # starting the next job.  Previously this only paused between jobs,
        # which made a long video render appear unresponsive.
        self._runner.request_pause()
        self._queue.pause()
        self._emit_running_state()

    def resume_queue(self) -> None:
        self._runner.request_resume()
        self._queue.resume()
        self._emit_running_state()

    def cancel_current(self) -> None:
        self._runner.request_cancel()
        self._queue.cancel_current()

    def retry_job(self, job_id: str) -> None:
        self._queue.retry(job_id)

    def remove_job(self, job_id: str) -> None:
        self._queue.remove(job_id)

    def clear_completed(self) -> None:
        self._queue.clear_completed()

    def get_job(self, job_id: str) -> Optional[VideoExportJob]:
        legacy = self._queue.get_job(job_id)
        if legacy is None:
            return None
        return VideoExportJob.from_legacy_job(legacy)

    def snapshot_jobs(self) -> List[VideoExportJob]:
        return [VideoExportJob.from_legacy_job(j) for j in self._queue.jobs]

    def restore_from_disk(self) -> None:
        """Load persisted jobs and mark any needing relink.

        Called by the workspace on startup once the controller's project
        context is set. Jobs whose timeline can no longer be resolved are
        flipped to :data:`VideoJobStatus.NEEDS_RELINK` so the UI can prompt
        the user instead of silently failing. NEEDS_RELINK is a real status
        on :class:`ExportQueue` now, so such jobs are never auto-run.
        """
        from src.features.deliver.core.export_queue import load_export_queue

        loaded = load_export_queue()
        for legacy in loaded:
            # AUD-11909: a single bad snapshot (duplicate job_id from a
            # partially updated queue file, malformed fields, ...) must not
            # bubble out of restore and kill the project-switch flow. Skip
            # the offending entry and keep restoring the rest.
            try:
                job = VideoExportJob.from_legacy_job(legacy)
                if not job.timeline_id:
                    # Nothing to relink against; leave as-is.
                    self._queue.enqueue(job.to_legacy_job(), preserve_status=False)
                    continue
                seq = self._sequences_by_id.get(job.timeline_id)
                if seq is None:
                    # Timeline gone — mark NEEDS_RELINK so it never auto-runs.
                    # We set the status on the legacy job (the queue's currency)
                    # AND in the snapshot so the typed view and the queue agree.
                    job.status = VideoJobStatus.NEEDS_RELINK
                    job.error = "Timeline not found in the current project"
                    legacy = job.to_legacy_job()
                    legacy.status = JobStatus.NEEDS_RELINK
                    self._queue.enqueue(legacy, preserve_status=True)
                else:
                    self._queue.enqueue(legacy, preserve_status=False)
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                logger.warning(
                    "Skipping unrestorable persisted job %s: %s",
                    getattr(legacy, "job_id", "<unknown>"),
                    e,
                )
        self.jobs_changed.emit()

    # =========================================================================
    # Context resolution (JobContextProvider)
    # =========================================================================

    def _resolve_context(self, job: VideoExportJob) -> JobContext:
        """Resolve the live project + sequence for a job."""
        if self._active_project is None:
            return JobContext(reason="No project is open.")

        if not job.timeline_id:
            return JobContext(reason="The task has no timeline reference.")

        seq = self._sequences_by_id.get(job.timeline_id)
        if seq is None:
            return JobContext(
                reason=f"Timeline “{job.timeline_name or job.timeline_id}” was not found in the project.",
            )

        # M10.3：入队快照 revision 校验——Deliver 页内编辑提交后工程
        # revision 递增，旧快照静默渲染会交付过期内容。快照 revision
        # >0 且与当前不一致 → 拒绝并明示（重新入队由用户决定）；
        # 快照 revision=0（旧队列/无版本快照）不拒（向后兼容）。
        current_revision = int(
            getattr(
                self._active_project, "revision",
                self._active_project_revision,
            ) or 0
        )
        job_revision = int(getattr(job, "project_revision", 0) or 0)
        if job_revision > 0 and job_revision != current_revision:
            return JobContext(
                reason=(
                    f"工程已修改（入队时 revision {job_revision} → "
                    f"当前 {current_revision}），任务快照过期——"
                    "请删除后重新入队。"
                ),
            )

        return JobContext(
            project=self._active_project,
            sequence=seq,
            project_id=self._active_project_id,
            # R01：revision 从工程对象实时读取——Deliver 页内编辑提交后
            # revision 递增，导出上下文必须与实际内容版本一致。
            project_revision=current_revision,
            # U02（第六轮审计）：嵌套序列导出需要全量时间线 map 做子序列
            # 递归解析；此前 Runner 调 export() 不带该参数，worker 报
            # “child sequence resolver is unavailable”。
            timeline_sequences=dict(self._sequences_by_id),
        )

    # =========================================================================
    # Queue signal handlers
    # =========================================================================

    def _on_queue_struct_change(self, *_args: Any) -> None:
        self.jobs_changed.emit()
        self._emit_running_state()

    def _on_queue_progress(self, job_id: str, _progress: float) -> None:
        self.job_progress.emit(job_id)

    def _on_job_completed(self, job_id: str, success: bool) -> None:
        self.job_finished.emit(job_id, success)
        self.jobs_changed.emit()
        self._emit_running_state()

    def _on_queue_drained(self) -> None:
        self._emit_running_state()
        # N12 修复（审计 2026-10-25 §2 N12）：①只测**本轮入队**的成功
        # 产物（add_jobs 批内跟踪），不混历史 DONE；②测量移专用 worker
        # 线程（旧实现在主线程槽同步逐文件完整解码——注释"主线程外"
        # 不符实现）；③代次计数拒旧（测量中新批入队 → 旧结果作废）；
        # ④测量失败显式 multi_version_qc_failed（未测/失败≠交付成功）。
        batch_ids = self._qc_batch_ids
        self._qc_batch_ids = set()
        try:
            outputs = [
                str(job.output_path)
                for job in self._queue.jobs
                if str(job.job_id) in batch_ids
                and str(getattr(job, "status", "")) == JobStatus.DONE
                and getattr(job, "output_path", "")
            ]
        except Exception:  # noqa: BLE001 — 快照失败不阻断状态通知
            outputs = []
        if not outputs:
            return
        if self._qc_worker is not None and self._qc_worker.isRunning():
            # 在途测量与新批并存：作废旧代（结果槽按代次拒收）
            self._qc_worker.cancel()
        self._qc_generation += 1
        generation = self._qc_generation
        worker = _QcMeasureWorker(outputs)
        self._qc_worker = worker
        _qc_worker_keepalive.add(worker)

        def _release_worker(w=worker) -> None:
            _qc_worker_keepalive.discard(w)
            w.deleteLater()

        worker.finished.connect(_release_worker)
        worker.finished_with_result.connect(
            self._qc_report_deliverer(generation))
        worker.start()

    def _qc_report_deliverer(self, generation: int):
        """结果投递闭包——控制器已销毁（关闭/测试拆除）时丢弃结果。"""
        def _deliver(report: dict) -> None:
            try:
                self._on_qc_measured(generation, report)
            except RuntimeError:
                # wrapped C/C++ object deleted——无人接收，丢弃
                logger.debug("QC report dropped: controller destroyed")
        return _deliver

    def _on_qc_measured(self, generation: int, report: dict) -> None:
        if generation != self._qc_generation:
            logger.debug("QC result generation %s stale (current %s) — "
                         "discarded", generation, self._qc_generation)
            return
        if self._qc_worker is not None:
            self._qc_worker.wait(5000)
        self.last_multi_version_qc = report
        self.multi_version_qc_ready.emit(dict(report))
        lufs = report.get("lufs") or {}
        failed = [path for path, entry in lufs.items()
                  if not isinstance(entry, dict)
                  or entry.get("input") is None
                  or entry.get("error")]
        if failed:
            self.multi_version_qc_failed.emit(failed)

    def _on_runner_aborted(self, job_id: str) -> None:
        """Runner aborted without a result (e.g. app quit) — park the queue."""
        logger.warning(
            "Export runner aborted without a result (job %s); pausing the queue",
            job_id,
        )
        self._queue.pause()
        self._emit_running_state()

    def _emit_running_state(self) -> None:
        self.queue_running_changed.emit(self._queue.is_running, self._queue.is_paused)
