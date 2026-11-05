"""R06-B: bake 服务编排（后台队列 + 生命周期 + 结果发布协议）。

职责（单点）：

- **提交**：能力 fail-closed 预检 → 钉住源帧与图快照（复制 ndarray +
  ``freeze_graph``）→ ``ColorBakeBroker.submit`` → 串行后台 ``QThread``
  队列执行后端 worker。
- **取消/过期/关工程**：显式取消触发后端协作取消（tile 边界）；图
  revision 前进 / 源变更由宿主转 ``broker.cancel_for_*``；会话切换
  （``reset``）= 全取消 + 队列排空 + ``BakeResultSession.reset()``
  （释放全部结果文件）。
- **接受**：worker 完成 → ``broker.accept_result(..., verify=token 校验)``
  → 接受则 ``BakeResultSession.register_result``（版本化条目）并 emit
  ``result_ready(task_id, uri)``（UI/控制器据此走一次 command 提交）；
  拒绝/取消/失败则释放暂存与钉住的源数组。
- **UI 信号**：``status_changed(ColorBakeStatus)``（含进度/错误码；
  不透传像素数据）。
"""
from __future__ import annotations

import copy
import logging
import threading
from typing import Any, Callable

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from src.features.color.core.color_bake_backend import (
    BakeBackendError, BakeBackendOutput, BakeCancelled,
    get_bake_backend_registry, request_active_cancel,
)
from src.features.color.core.color_bake_protocol import (
    ColorBakeBroker, ColorBakeRequest, ColorBakeStatus,
)
from src.features.color.core.color_bake_result_registry import (
    BakeResultSession, get_bake_result_session,
)

_LOGGER = logging.getLogger(__name__)

# 活动服务弱引用注册表：工程关闭的单一收口（on_project_closed）经
# ``reset_all_bake_services`` 取消在途任务并释放结果文件，无需各宿主
# 逐一挂钩。
_ACTIVE_SERVICES: "list[Any]" = []
_ACTIVE_SERVICES_LOCK = threading.Lock()


def reset_all_bake_services() -> int:
    """工程关闭/新建：全部活动服务取消在途 + 释放结果文件；返回数量。"""
    with _ACTIVE_SERVICES_LOCK:
        services = list(_ACTIVE_SERVICES)
    count = 0
    for service in services:
        try:
            service.reset()
            count += 1
        except Exception:  # noqa: BLE001 — 关闭路径不因单个服务失败中断
            _LOGGER.exception("bake service reset failed on project close")
    return count


class _BakeWorkerThread(QThread):
    """串行消费服务队列的专用线程（一次一个任务；协作取消）。

    队列与信号量都在 ``ColorBakeService`` 上（服务锁保护）；本类只做
    阻塞取件 → 执行。``_dequeue_task`` 返回 None 即退出哨兵（reset 清
    队后唤醒；后续 submit 经 ``_ensure_worker`` 重建线程）。
    """

    def __init__(self, service: "ColorBakeService") -> None:
        super().__init__()
        self._service = service

    def run(self) -> None:
        while True:
            task_id = self._service._dequeue_task(block=True)
            if task_id is None:
                return
            self._service._run_task(task_id)


class ColorBakeService(QObject):
    """bake 生命周期宿主（GUI 线程构造；推理在专用线程）。"""

    status_changed = pyqtSignal(object)          # ColorBakeStatus
    result_ready = pyqtSignal(str, str)          # task_id, bake_uri
    bake_failed = pyqtSignal(str, str)           # task_id, error_code

    def __init__(self, *, session: BakeResultSession | None = None,
                 broker: ColorBakeBroker | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._session = session or get_bake_result_session()
        self.broker = broker or ColorBakeBroker()
        self._thread_lock = threading.RLock()
        self._queue: list[str] = []
        self._queue_sem = threading.Semaphore(0)
        # task_id → 钉住的执行上下文（快照 + 源帧 + verify 回调）
        self._pinned: dict[str, dict[str, Any]] = {}
        self._worker_thread: _BakeWorkerThread | None = None
        with _ACTIVE_SERVICES_LOCK:
            _ACTIVE_SERVICES.append(self)

    # ── 队列基础设施 ───────────────────────────────────────────────
    def _queue_task(self, task_id: str) -> None:
        with self._thread_lock:
            self._queue.append(task_id)
        self._queue_sem.release()

    def _dequeue_task(self, *, block: bool) -> str | None:
        if block:
            self._queue_sem.acquire()
        elif not self._queue_sem.acquire(blocking=False):
            return None
        with self._thread_lock:
            if self._queue:
                return self._queue.pop(0)
        return None  # 唤醒但队列空 = 退出哨兵对齐 shutdown

    def _ensure_worker(self) -> None:
        if self._worker_thread is not None and self._worker_thread.isRunning():
            return
        self._worker_thread = _BakeWorkerThread(self)
        self._worker_thread.start()

    # ── 提交 ───────────────────────────────────────────────────────
    def submit_bake(self, request: ColorBakeRequest, *,
                    snapshot: Any = None, source_frame: Any = None,
                    verify: Callable[[], bool] | None = None) -> str | None:
        """登记并排队一次 bake；能力/输入不合法返回 None（已发失败状态）。"""
        registry = get_bake_backend_registry()
        worker, capability, reject = registry.resolve(request.backend_id)
        if reject:
            self.bake_failed.emit(request.task_id, reject)
            self.status_changed.emit(ColorBakeStatus(
                task_id=request.task_id, phase="failed",
                error_code=reject, backend_id=request.backend_id))
            return None
        if snapshot is None or source_frame is None:
            self.bake_failed.emit(request.task_id, "missing_input")
            self.status_changed.emit(ColorBakeStatus(
                task_id=request.task_id, phase="failed",
                error_code="missing_input", backend_id=request.backend_id))
            return None
        # 钉住执行上下文：快照冻结 + 源帧复制（防播放/编辑在途变更）
        import numpy as np
        pinned_source = copy.deepcopy(source_frame) \
            if not isinstance(source_frame, np.ndarray) \
            else np.array(source_frame, copy=True)
        self.broker.submit(request)
        with self._thread_lock:
            self._pinned[request.task_id] = {
                "request": request, "snapshot": snapshot,
                "source": pinned_source, "verify": verify}
        self._ensure_worker()
        self._queue_task(request.task_id)
        self.status_changed.emit(ColorBakeStatus(
            task_id=request.task_id, phase="queued",
            backend_id=request.backend_id))
        return request.task_id

    # ── 执行（工作线程） ───────────────────────────────────────────
    def _run_task(self, task_id: str) -> None:
        with self._thread_lock:
            context = self._pinned.get(task_id)
        if context is None:
            return  # 提交后被取消/清理
        request: ColorBakeRequest = context["request"]
        registry = get_bake_backend_registry()
        worker, _cap, reject = registry.resolve(request.backend_id)
        if reject or worker is None:
            self._finish_failure(task_id, reject or "backend_unregistered")
            return
        self.status_changed.emit(ColorBakeStatus(
            task_id=task_id, phase="running", percent=0.0,
            backend_id=request.backend_id))
        try:
            output: BakeBackendOutput = worker(
                request, context["snapshot"], context["source"],
                lambda: self.broker.was_cancelled(task_id),
                lambda pct: self.status_changed.emit(ColorBakeStatus(
                    task_id=task_id, phase="running", percent=pct,
                    backend_id=request.backend_id)))
        except BakeCancelled:
            self._discard(task_id, "cancelled")
            return
        except BakeBackendError as exc:
            self._finish_failure(task_id, exc.code)
            return
        except Exception as exc:  # noqa: BLE001 — 后端未编码异常兜底
            _LOGGER.exception("bake backend crashed: %s", exc)
            self._finish_failure(task_id, "backend_exec_failed")
            return
        # 接受守卫：在途 + verify token（会话/代际/栈/节点/实例仍匹配）
        verify = context.get("verify")
        cancelled_before = self.broker.was_cancelled(task_id)
        accepted = self.broker.accept_result(
            task_id, output, verify=verify)
        if accepted is None:
            # broker 把 verify 失败也标记为 cancelled——用进入 accept 前
            # 的状态区分可观测语义：显式/过期取消只发 cancelled 状态；
            # verify 失败（目标已删除等）额外发 bake_failed(verify_failed)。
            self._discard(task_id, "cancelled",
                          failed_code=None if cancelled_before
                          else "verify_failed")
            return
        entry = self._session.register_result(
            output.payload, output.format_hint,
            metadata={**dict(output.metadata),
                      "task_id": task_id,
                      "target": dict(request.target),
                      "resource_id": request.resource_id,
                      "spec_version": request.spec_version})
        self._release_pinned(task_id)
        self.result_ready.emit(task_id, entry.uri)
        self.status_changed.emit(ColorBakeStatus(
            task_id=task_id, phase="succeeded", percent=100.0,
            backend_id=request.backend_id, bake_uri=entry.uri))

    def _finish_failure(self, task_id: str, code: str) -> None:
        self.broker.cancel(task_id)
        self._release_pinned(task_id)
        self.bake_failed.emit(task_id, code)
        self.status_changed.emit(ColorBakeStatus(
            task_id=task_id, phase="failed", error_code=code))

    def _discard(self, task_id: str, phase: str,
                 *, failed_code: str | None = None) -> None:
        """取消/过期丢弃：释放钉住的源数组（暂存无条目可删）。"""
        self._release_pinned(task_id)
        if failed_code:
            self.bake_failed.emit(task_id, failed_code)
        self.status_changed.emit(ColorBakeStatus(
            task_id=task_id, phase=phase))

    def _release_pinned(self, task_id: str) -> None:
        with self._thread_lock:
            context = self._pinned.pop(task_id, None)
        if context is not None:
            context["source"] = None
            context["snapshot"] = None

    # ── 取消/关工程 ────────────────────────────────────────────────
    def cancel(self, task_id: str) -> bool:
        """显式取消：broker 标记 + 后端协作取消（tile 边界生效）。"""
        request_active_cancel(task_id)
        return self.broker.cancel(task_id)

    def reset(self, *, join_worker_timeout: float = 2.0) -> None:
        """关工程/换会话：全取消 + 清队 + 释放全部结果文件。

        在途任务由协作取消让出（tile 边界；不 terminate 原生推理）；
        队列唤醒哨兵让空闲工作线程退出并 join（有界等待——测试收尾
        的确定性线程回收；超时仅记录，不阻塞关工程），后续 submit
        重建线程。
        """
        with self._thread_lock:
            pending = list(self._pinned)
            self._queue.clear()
        for task_id in pending:
            self.cancel(task_id)
        self._queue_sem.release()
        worker = self._worker_thread
        if worker is not None and worker is not QThread.currentThread():
            if not worker.wait(int(join_worker_timeout * 1000)):
                _LOGGER.warning(
                    "bake worker did not exit within %.1fs on reset",
                    join_worker_timeout)
        self._session.reset()

    def active_task_ids(self) -> list[str]:
        return [request.task_id for request in self.broker.pending()]
