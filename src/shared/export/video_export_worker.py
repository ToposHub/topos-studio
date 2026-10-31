"""
视频导出后台 Worker — 在独立 QThread 中运行 TimelineExporter。

设计目标
========

- 不阻塞 UI：整个逐帧渲染+编码循环在 worker 线程执行
- 进度反馈：通过 progress 信号实时回传 (current_frame, total_frames, percent)
- 可取消：主线程调 cancel()，导出循环在下一帧检查并退出
- 线程安全：TimelineExporter 的渲染/编码循环在 worker 线程执行；
  GPU 路径（P0-GPU-1 合成、P1-COLOR-13 降噪、comp_clip）使用同线程
  惰性创建的 headless GL context，失败自动回落 CPU，导出不中断

架构
====

    ┌──────────────────────────────────────────────────┐
    │ 主线程（UI）                                       │
    │  VideoExporter.export(...)                        │
    │   ├─ 构造 TimelineExportConfig                    │
    │   ├─ 创建 TimelineExporter                        │
    │   ├─ connect worker.{finished,error,progress}     │
    │   └─ QMetaObject.invokeMethod(worker, 'run', ...) │
    │                  │（跨线程调用，Qt 自动队列）         │
    └──────────────────┼───────────────────────────────┘
                       ▼
    ┌──────────────────────────────────────────────────┐
    │ Worker 线程                                       │
    │  VideoExportWorker.run(config)                    │
    │   ├─ timeline_exporter.export_timeline(config)    │
    │   │    └─ progress_callback → progress.emit(...)  │
    │   └─ finished.emit(path) / error.emit(msg)        │
    └──────────────────────────────────────────────────┘
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot, QMetaObject, Qt, Q_ARG

logger = logging.getLogger(__name__)


class VideoExportWorker(QObject):
    """
    视频导出 Worker —— 实际执行 TimelineExporter 的对象。

    通过 moveToThread 移到 worker 线程，主线程通过
    QMetaObject.invokeMethod 触发 run() 槽函数，从而在 worker 线程内执行。

    Signals:
        finished(str):  导出成功，携带输出路径
        error(str):     导出失败，携带错误消息
        progress(int, int, float):  当前帧、总帧数、进度百分比(0-100)
    """

    finished = pyqtSignal(str)
    error = pyqtSignal(str)
    progress = pyqtSignal(int, int, float)

    def __init__(self) -> None:
        super().__init__()
        # 由 run() 中创建的 TimelineExporter，cancel() 时引用
        self._exporter = None  # type: Optional[object]
        self._config = None
        self._post_export: Optional[Callable[[str], object]] = None
        # P1-02: 嵌套序列导出的全量时间线（export() 调用方注入）
        self._timeline_sequences: Optional[dict] = None
        # Controls may arrive before TimelineExporter has been constructed
        # on the worker thread.  Keep the request here and apply it as soon
        # as the real exporter becomes available.
        self._pause_requested = False
        self._cancel_requested = False
        # P0-05/P1-02：最近一次导出的结构化结果（成功路径可查 audio_omitted）
        self.last_outcome = None

    @pyqtSlot(object, object, object)
    def run(self, project, sequence, config) -> None:
        """
        在 worker 线程内执行导出。

        Args:
            project: EditProject 实例
            sequence: TimelineSequence 实例
            config: TimelineExportConfig 实例
        """
        try:
            # 延迟导入，避免主线程启动时加载 PyAV
            from src.features.edit.core.timeline_export import (
                create_timeline_exporter,
            )

            self._config = config

            def on_progress(current: int, total: int, percent: float) -> None:
                # TimelineExporter 在当前线程内调用此回调，
                # 通过信号把进度转发到主线程（Qt 自动跨线程队发信号）
                self.progress.emit(current, total, percent * 100.0)

            exporter = create_timeline_exporter(
                project=project,
                sequence=sequence,
                progress_callback=on_progress,
                timeline_sequences=getattr(self, "_timeline_sequences", None),
            )
            self._exporter = exporter
            if self._cancel_requested:
                exporter.cancel_export()
            if self._pause_requested:
                exporter.pause_export()

            outcome = exporter.export_timeline(config)

            # P0-05：三态显式——只有用户取消才报 EXPORT_CANCELLED，
            # 失败携带结构化错误码与 cause（此前一切 False 都被吞成取消）。
            if outcome.success:
                if self._post_export is not None:
                    try:
                        # T-P0-20：回调签名支持时附带统一阶段 telemetry
                        #（job manifest → topos.get_job_status 可读）。
                        stage_telemetry = getattr(
                            outcome, "stage_telemetry", None)
                        try:
                            import inspect as _inspect

                            accepts_telemetry = (
                                "stage_telemetry" in _inspect.signature(
                                    self._post_export).parameters
                                or any(
                                    p.kind == _inspect.Parameter.VAR_KEYWORD
                                    for p in _inspect.signature(
                                        self._post_export).parameters.values()
                                )
                            )
                        except (TypeError, ValueError):
                            accepts_telemetry = False
                        if accepts_telemetry:
                            verdict = self._post_export(
                                config.output_path,
                                stage_telemetry=stage_telemetry,
                            )
                        else:
                            verdict = self._post_export(config.output_path)
                        if isinstance(verdict, tuple):
                            accepted = bool(verdict[0])
                            message = str(verdict[1]) if len(verdict) > 1 else ""
                        else:
                            accepted = bool(verdict)
                            message = ""
                    except Exception as exc:  # noqa: BLE001 — post-check is a hard gate
                        logger.exception("post-export verification failed")
                        accepted = False
                        message = f"EXPORT_POSTCHECK_FAILED: {type(exc).__name__}"
                    if not accepted:
                        self.error.emit(message or "EXPORT_POSTCHECK_FAILED")
                        return
                # P1-02：请求了音频但编码器无音轨（GIF/图片序列；topos
                # 容器 v1.1 起已带音轨，不再触发）——结果在
                # ExportOutcome.audio_omitted（机器可读）；payload 保持纯路径
                #（消费方按文件路径使用）。
                if getattr(outcome, 'audio_omitted', False):
                    logger.warning(
                        "导出完成但音频未写入（audio_omitted）：%s 不含音轨，"
                        "输出 %s 无音频",
                        getattr(config.codec, "value", config.codec),
                        config.output_path,
                    )
                # W6-02：硬件编码回退不阻塞导出，但必须显式留痕——
                # reason 进 outcome（job/UI 层机器可读），此处同步落日志
                hw_reason = getattr(outcome, "hw_fallback_reason", "")
                if hw_reason:
                    logger.warning("导出完成（硬件编码回退 CPU：%s）", hw_reason)
                self.last_outcome = outcome
                self.finished.emit(config.output_path)
            elif outcome.cancelled:
                self.error.emit("EXPORT_CANCELLED")
            else:
                detail = f"{outcome.error_code}: {outcome.cause}" if outcome.cause \
                    else (outcome.error_code or "EXPORT_FAILED")
                self.error.emit(detail)

        except Exception as e:
            logger.exception("Video export failed")
            self.error.emit(str(e))
        finally:
            self._exporter = None
            self._config = None

    def request_cancel(self) -> None:
        """请求取消导出（主线程调用，线程安全）。"""
        self._cancel_requested = True
        self._pause_requested = False
        exporter = self._exporter
        if exporter is not None:
            try:
                exporter.cancel_export()  # type: ignore[attr-defined]
            except Exception as e:
                logger.warning(f"cancel_export failed: {e}")

    def request_pause(self) -> None:
        """请求在当前帧完成后暂停；可早于 worker 初始化到达。"""
        self._pause_requested = True
        exporter = self._exporter
        if exporter is not None:
            try:
                exporter.pause_export()  # type: ignore[attr-defined]
            except Exception as e:
                logger.warning(f"pause_export failed: {e}")

    def request_resume(self) -> None:
        """恢复暂停的导出。"""
        self._pause_requested = False
        exporter = self._exporter
        if exporter is not None:
            try:
                exporter.resume_export()  # type: ignore[attr-defined]
            except Exception as e:
                logger.warning(f"resume_export failed: {e}")


class VideoExporter(QObject):
    """
    视频导出协调器 —— 持有 worker 与 thread，提供主线程 API。

    使用方式::

        exporter = VideoExporter()
        exporter.export(
            project=edit_project,
            sequence=timeline_sequence,
            config=config,
            on_finished=on_finished_cb,
            on_error=on_error_cb,
            on_progress=on_progress_cb,
        )
        # 需要取消时：
        exporter.cancel()
        # 导出完成后（在 on_finished/on_error 回调里）：
        exporter.cleanup()
    """

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)

        self._worker_thread = QThread()
        self._worker = VideoExportWorker()
        self._worker.moveToThread(self._worker_thread)

        # 把 worker 信号转发出去（供 controller 直接 connect）
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.error.connect(self._on_worker_error)
        self._worker.progress.connect(self._on_worker_progress)

        self._worker_thread.start()

        # 用户回调（由 export() 设置）
        self._on_finished: Optional[Callable[[str], None]] = None
        self._on_error: Optional[Callable[[str], None]] = None
        self._on_progress: Optional[Callable[[int, int, float], None]] = None

        # 是否正在导出
        self._is_exporting: bool = False

    @property
    def is_exporting(self) -> bool:
        """是否正在导出。"""
        return self._is_exporting

    def export(
        self,
        project,
        sequence,
        config,
        on_finished: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_progress: Optional[Callable[[int, int, float], None]] = None,
        on_post_export: Optional[Callable[[str], object]] = None,
        timeline_sequences: Optional[dict] = None,
    ) -> None:
        """
        启动后台导出。

        Args:
            project: EditProject 实例
            sequence: TimelineSequence 实例
            config: TimelineExportConfig 实例
            on_finished: 成功回调，签名 (output_path: str) -> None
            on_error: 失败回调，签名 (error_msg: str) -> None
                      特殊值 "EXPORT_CANCELLED" 表示用户取消
            on_progress: 进度回调，签名 (current, total, percent) -> None
            on_post_export: 编码完成后、finished 信号发出前运行的质量门回调。
                            返回 bool 或 (bool, message)，在 worker 线程执行。
        """
        if self._is_exporting:
            logger.warning("VideoExporter.export() called while already exporting")
            return

        self._on_finished = on_finished
        self._on_error = on_error
        self._on_progress = on_progress
        self._worker._cancel_requested = False
        self._worker._pause_requested = False
        self._worker._post_export = on_post_export
        # P1-02: 嵌套序列导出需要的全量时间线（经普通属性传递，run 槽签名不变）
        self._worker._timeline_sequences = timeline_sequences
        self._is_exporting = True

        # 通过 invokeMethod 把调用投递到 worker 线程
        # 注意：必须用 QueuedConnection，否则会在主线程直接执行
        QMetaObject.invokeMethod(
            self._worker,
            "run",
            Qt.ConnectionType.QueuedConnection,
            Q_ARG(object, project),
            Q_ARG(object, sequence),
            Q_ARG(object, config),
        )
    def cancel(self) -> None:
        """请求取消导出。实际的取消会在下一帧生效。"""
        self._worker.request_cancel()

    def pause(self) -> None:
        """请求暂停导出；当前帧结束后生效。"""
        self._worker.request_pause()

    def resume(self) -> None:
        """恢复暂停的导出。"""
        self._worker.request_resume()

    def cleanup(self) -> None:
        """清理 worker 线程资源（在 finished/error 之后调用）。

        AUD-33600：若 cleanup() 在 run() 仍在执行时被调用（导出暂停门
        每 0.25s 轮询、或单帧渲染/编码超过 3 秒——4K 导出常态），不先
        请求取消的话 run() 会继续渲染/写输出，wait() 超时被静默忽略；
        随后 QThread 带着运行中的线程析构会触发 Qt qFatal（"QThread:
        Destroyed while thread is still running"）进程中止。因此：
        (1) 先 request_cancel() 让导出循环在下一帧退出，wait 才有机会
            成功；
        (2) 检查 wait() 返回值——超时绝不静默放行，升级 requestInterruption
            + 二次等待后以 error 级日志留痕。
        """
        try:
            # 先请求取消：run() 内的导出循环在下一帧检查并退出
            self._worker.request_cancel()
            self._worker_thread.quit()
            if self._worker_thread.wait(3000):
                return
            # 超时不放行：请求中断并二次等待，仍失败则显式留痕
            self._worker_thread.requestInterruption()
            if not self._worker_thread.wait(2000):
                logger.error(
                    "VideoExporter cleanup: worker 线程 5s 内未退出；"
                    "QThread 析构可能触发 qFatal 崩溃（Destroyed while "
                    "thread is still running）"
                )
        except Exception as e:
            logger.warning(f"VideoExporter cleanup failed: {e}")

    # ------------------------------------------------------------------
    # 内部槽：在主线程接收 worker 信号
    # ------------------------------------------------------------------

    def _on_worker_finished(self, output_path: str) -> None:
        self._is_exporting = False
        if self._on_finished is not None:
            try:
                self._on_finished(output_path)
            except Exception as e:
                logger.exception(f"on_finished callback error: {e}")

    def _on_worker_error(self, error_msg: str) -> None:
        self._is_exporting = False
        if self._on_error is not None:
            try:
                self._on_error(error_msg)
            except Exception as e:
                logger.exception(f"on_error callback error: {e}")

    def _on_worker_progress(self, current: int, total: int, percent: float) -> None:
        if self._on_progress is not None:
            try:
                self._on_progress(current, total, percent)
            except Exception as e:
                logger.exception(f"on_progress callback error: {e}")
