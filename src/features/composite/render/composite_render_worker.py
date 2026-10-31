"""Background worker for composite video / image-sequence rendering.

Mirrors the architecture of src/shared/export/video_export_worker.py but
drives CompositeRenderOutput instead of TimelineExporter. Runs entirely on
a worker thread; progress/finished/error are forwarded to the main thread
via Qt signals (the only thread-safe way to cross thread boundaries).

    ┌────────────────────────────────────────────────────────────┐
    │ Main thread (UI)                                           │
    │  CompositeRenderExporter.export(...)                       │
    │   ├─ build CompositeRenderOutput + settings                │
    │   ├─ connect worker.{finished,error,progress}              │
    │   └─ QMetaObject.invokeMethod(worker, 'run', ...)          │
    └────────────────────────────┬───────────────────────────────┘
                                 ▼
    ┌────────────────────────────────────────────────────────────┐
    │ Worker thread                                              │
    │  CompositeRenderWorker.run(...)                            │
    │   ├─ CompositeRenderOutput.render_video/sequence           │
    │   │    └─ progress_cb → progress.emit(current, total)      │
    │   └─ finished.emit(output_path) / error.emit(msg)          │
    └────────────────────────────────────────────────────────────┘

GL threading: CompositeRenderOutput → CompositeFrameRenderer creates its
own GL context via moderngl.create_context() on the worker thread, so it
is NOT bound to the main thread's context (same model as the timeline
VideoExportWorker path which renders comp-clip frames off the main thread).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Optional

from PyQt6.QtCore import (
    Q_ARG,
    QObject,
    QThread,
    Qt,
    QMetaObject,
    pyqtSignal,
    pyqtSlot,
)

logger = logging.getLogger(__name__)


class CompositeRenderWorker(QObject):
    """Worker that runs a composite video/sequence render off the main thread.

    Signals:
        finished(str): render succeeded, carries the output path/dir.
        error(str): render failed, carries an error message
            (``"EXPORT_CANCELLED"`` means the user cancelled).
        progress(int, int): (frames_written, total_frames).
    """

    finished = pyqtSignal(str)
    error = pyqtSignal(str)
    progress = pyqtSignal(int, int)

    def __init__(self) -> None:
        super().__init__()
        self._cancel_requested = False
        self._cancel_event = threading.Event()
        self._args: Optional[dict] = None

    @pyqtSlot(object, object)
    def run(self, settings: Dict[str, Any], context: Dict[str, Any]) -> None:
        """Execute the render on this worker thread.

        Args:
            settings: CompositeRenderDialog.get_settings() output.
            context: dict with graph_data, edit_project, timeline_sequences,
                comp_clip, width, height (captured on the main thread).
        """
        try:
            from src.features.composite.render.composite_render_output import (
                CompositeRenderOutput,
            )

            # config_manager drives color-engine selection (builtin vs OCIO)
            # for the whole render — passed through from the controller so the
            # export path honours composite.color_engine (P1 fix).
            config_manager = context.get("config_manager")
            renderer = CompositeRenderOutput(config_manager=config_manager)

            def on_progress(current: int, total: int) -> None:
                # Called inline on THIS thread; re-emit as a signal so the
                # main-thread slot receives it safely (Qt queues signals
                # across thread boundaries; direct calls would not).
                self.progress.emit(current, total)

            kind = settings.get("kind", "video_file")
            output_descriptor = context.get("output_descriptor")
            if kind == "video_file":
                result = renderer.render_video(
                    graph_data=context["graph_data"],
                    output_path=settings["output_path"],
                    width=settings["width"],
                    height=settings["height"],
                    fps=settings["fps"],
                    start_frame=settings["start_frame"],
                    end_frame=settings["end_frame"],
                    codec=settings.get("codec", "h264"),
                    pix_fmt=settings.get("pix_fmt", "yuv420p"),
                    crf=settings.get("crf"),
                    preset=settings.get("preset"),
                    output_descriptor=output_descriptor,
                    edit_project=context.get("edit_project"),
                    timeline_sequences=context.get("timeline_sequences"),
                    comp_clip=context.get("comp_clip"),
                    render_pass=settings.get("render_pass", "beauty"),
                    progress_cb=on_progress,
                    is_cancelled=self._cancel_event.is_set,
                    parallel_frames=settings.get("parallel_frames"),
                )
            else:  # image_sequence
                result = renderer.render_image_sequence(
                    graph_data=context["graph_data"],
                    output_dir=settings["output_path"],
                    base_name=settings["base_name"],
                    ext=settings.get("sequence_format", "png"),
                    start_frame=settings["start_frame"],
                    end_frame=settings["end_frame"],
                    image_options=settings.get("options") or {},
                    output_descriptor=output_descriptor,
                    edit_project=context.get("edit_project"),
                    timeline_sequences=context.get("timeline_sequences"),
                    comp_clip=context.get("comp_clip"),
                    render_pass=settings.get("render_pass", "beauty"),
                    progress_cb=on_progress,
                    is_cancelled=self._cancel_event.is_set,
                    parallel_frames=settings.get("parallel_frames"),
                )

            if result.success:
                self.finished.emit(result.output_path)
            elif result.error == "EXPORT_CANCELLED":
                self.error.emit("EXPORT_CANCELLED")
            else:
                self.error.emit(result.error or "Render failed")
        except Exception as e:
            logger.exception("Composite render worker failed")
            self.error.emit(str(e))

    def request_cancel(self) -> None:
        """Request cancellation (called from the main thread, thread-safe)."""
        self._cancel_requested = True
        self._cancel_event.set()

    def reset_cancel(self) -> None:
        """Clear cancellation state before a new one-shot export."""
        self._cancel_requested = False
        self._cancel_event.clear()


class CompositeRenderExporter(QObject):
    """Coordinator holding the worker + thread; main-thread API.

    Usage::

        exporter = CompositeRenderExporter()
        exporter.export(
            settings=settings,
            context=context,
            on_finished=on_finished_cb,
            on_error=on_error_cb,
            on_progress=on_progress_cb,
        )
        # to cancel:
        exporter.cancel()
        # when done (inside on_finished/on_error):
        exporter.cleanup()

    AUD-81010 lifecycle contract: the auxiliary thread's event loop stops
    after the FIRST terminal signal (finished/error) so an idle QThread is
    never left running, which makes the exporter effectively ONE-SHOT.
    After termination, ``export()`` refuses the task and reports through
    ``on_error`` instead of silently posting a "run" invocation onto a
    quit (or already deleteLater'd) worker. Create a new exporter per
    export task — the controller already does.
    """

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._worker_thread = QThread()
        self._worker = CompositeRenderWorker()
        self._worker.moveToThread(self._worker_thread)

        self._worker.finished.connect(self._on_worker_finished)
        self._worker.error.connect(self._on_worker_error)
        self._worker.progress.connect(self._on_worker_progress)
        # The worker has a manual run slot; once it emits a terminal signal,
        # stop the auxiliary Qt event loop even if the caller delays cleanup.
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.error.connect(self._worker_thread.quit)
        self._worker_thread.finished.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._on_worker_thread_finished)

        self._worker_thread.start()

        self._on_finished: Optional[Callable[[str], None]] = None
        self._on_error: Optional[Callable[[str], None]] = None
        self._on_progress: Optional[Callable[[int, int], None]] = None
        self._is_exporting: bool = False
        # AUD-81010: True once the worker thread reached its terminal
        # state — the event loop has quit (or is quitting) and the worker
        # object is pending deleteLater, so no further task can run.
        self._terminated: bool = False

    @property
    def is_exporting(self) -> bool:
        return self._is_exporting

    @property
    def is_terminated(self) -> bool:
        """True after the worker's first task finished (one-shot worker)."""
        return self._terminated

    def export(
        self,
        settings: Dict[str, Any],
        context: Dict[str, Any],
        on_finished: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        """Start a background render.

        Args:
            settings: CompositeRenderDialog.get_settings().
            context: dict with graph_data, edit_project, timeline_sequences,
                comp_clip (captured on the main thread before this call).
            on_finished: (output_path) -> None.
            on_error: (error_msg) -> None; "EXPORT_CANCELLED" = user cancel.
            on_progress: (current, total) -> None.
        """
        if self._is_exporting:
            logger.warning("CompositeRenderExporter.export() called while busy")
            return
        if self._terminated:
            # AUD-81010: the worker thread quit after its first task (and
            # the worker object is being deleteLater'd) — a queued "run"
            # here would be silently dropped or hit a dangling QObject.
            # Refuse loudly instead of losing the task.
            logger.error(
                "CompositeRenderExporter.export() called after the worker "
                "terminated; create a new exporter for each export task")
            if on_error is not None:
                try:
                    on_error(
                        "Export worker already terminated; "
                        "please restart the export")
                except Exception as e:
                    logger.exception(f"on_error callback error: {e}")
            return

        self._on_finished = on_finished
        self._on_error = on_error
        self._on_progress = on_progress
        self._is_exporting = True
        self._worker.reset_cancel()

        QMetaObject.invokeMethod(
            self._worker,
            "run",
            Qt.ConnectionType.QueuedConnection,
            Q_ARG(object, settings),
            Q_ARG(object, context),
        )

    def cancel(self) -> None:
        """Request cancellation (takes effect at the next frame)."""
        self._worker.request_cancel()

    def cleanup(self, timeout_ms: int = 3000) -> bool:
        """Tear down the worker thread after requesting a cooperative stop."""
        if not self._worker_thread.isRunning():
            self._is_exporting = False
            return True
        try:
            if self._is_exporting:
                self._worker.request_cancel()
            self._worker_thread.quit()
            stopped = self._worker_thread.wait(max(0, int(timeout_ms)))
            if not stopped:
                logger.error(
                    "Composite export worker did not stop within %d ms; "
                    "keeping it alive for cooperative cancellation", timeout_ms)
                return False
            self._is_exporting = False
            return True
        except Exception as e:
            logger.warning(f"CompositeRenderExporter cleanup failed: {e}")
            return False

    # ── Internal slots (run on main thread, receiving worker signals) ──

    def _on_worker_thread_finished(self) -> None:
        """Mark the exporter as terminated (AUD-81010 one-shot contract)."""
        self._terminated = True
        self._is_exporting = False

    def _on_worker_finished(self, output_path: str) -> None:
        # These slots are queued BEFORE the connected _worker_thread.quit,
        # so setting _terminated here also closes the tiny window where
        # _is_exporting is already False but the quit event has not yet
        # stopped the event loop.
        self._terminated = True
        self._is_exporting = False
        if self._on_finished is not None:
            try:
                self._on_finished(output_path)
            except Exception as e:
                logger.exception(f"on_finished callback error: {e}")

    def _on_worker_error(self, error_msg: str) -> None:
        self._terminated = True
        self._is_exporting = False
        if self._on_error is not None:
            try:
                self._on_error(error_msg)
            except Exception as e:
                logger.exception(f"on_error callback error: {e}")

    def _on_worker_progress(self, current: int, total: int) -> None:
        if self._on_progress is not None:
            try:
                self._on_progress(current, total)
            except Exception as e:
                logger.exception(f"on_progress callback error: {e}")
