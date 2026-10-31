"""
跟踪工作线程（阶段 3）。

``TrackingWorker`` 是 ``QThread``，在工作线程内直接运行无 Qt 依赖的
``Tracker2D.track``，通过信号把进度与结果送回 UI 线程：

- ``progress``：(generation, done, total)
- ``finished_with_result``：(generation, TrackResult)，结果携带任务代次
- ``failed_with_error``：(generation, message)，跟踪过程抛出异常时

取消通过 ``request_cancel()`` 置位内部 token，跟踪主循环在每帧边界检查。
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

from src.shared.tracking.tracker_2d import Tracker2D, TrackRequest

logger = logging.getLogger(__name__)


class _CancelToken:
    """跨线程取消 token（AUD-24913）。

    内部用 ``threading.Event`` 承载置位状态：取消来自 UI 线程、
    检查发生在 worker 线程，Event 提供带内存语义的原子置位/查询，
    代替依赖 GIL 实现细节的裸布尔属性。对外接口（``set`` /
    ``is_set``）与 ``Tracker2D.track`` 的 cancel_token 协议兼容。
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()


class TrackingWorker(QThread):
    """单方向跟踪任务线程。

    用法：
        worker = TrackingWorker(request, tracker)
        worker.progress.connect(...)
        worker.finished_with_result.connect(...)
        worker.start()
        ... worker.request_cancel()
    """

    progress = pyqtSignal(int, int, int)          # generation, done, total
    finished_with_result = pyqtSignal(int, object)  # generation, TrackResult
    failed_with_error = pyqtSignal(int, str)        # generation, message

    def __init__(
        self,
        request: TrackRequest,
        tracker: Optional[Tracker2D] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("TrackingWorker")
        self.request = request
        self.tracker = tracker or Tracker2D()
        self._cancel_token = _CancelToken()

    # ---- 控制 ----

    @property
    def generation(self) -> int:
        return self.request.generation

    def request_cancel(self) -> None:
        """请求取消（尽快在下一帧边界生效）。"""
        self._cancel_token.set()

    def cancel_requested(self) -> bool:
        return self._cancel_token.is_set()

    # ---- 执行 ----

    def run(self) -> None:  # noqa: D102 - QThread 入口
        gen = self.request.generation
        try:
            result = self.tracker.track(
                self.request,
                cancel_token=self._cancel_token,
                progress_cb=self._on_progress,
            )
        except Exception as exc:  # noqa: BLE001 - 异常经信号送回 UI 线程
            logger.exception("跟踪任务异常 (generation=%s)", gen)
            self.failed_with_error.emit(gen, str(exc))
            return
        self.finished_with_result.emit(gen, result)

    def _on_progress(self, done: int, total: int) -> None:
        self.progress.emit(self.request.generation, done, total)
