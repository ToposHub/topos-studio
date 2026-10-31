"""可取消的后台 3D 模型解析服务（P1-25/P2-03）。

模型解析只做 CPU、numpy 和文件 I/O，不应阻塞 Qt 主线程，也不应在 worker
线程触碰 ModernGL。这个模块把导入拆成两个明确阶段：

* worker：运行时预检 + ``model_loader.load_model``，产出纯 Python
  ``SceneGraphRoot``；
* render thread：消费 ``ModelImportResult.root``，创建/上传 VAO、纹理等 GL 资源。

取消采用合作式语义：正在执行的第三方解析库无法被安全强杀，收到取消后会丢弃
结果；排队中的 Future 则直接取消。这样不会把半成品 SceneGraph 或 GL 对象交给
调用方，也不会因为关闭项目而留下未管理的资源。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Set

from .format_capabilities import FormatImportProbe, probe_import

logger = logging.getLogger(__name__)


IMPORT_QUEUED = "queued"
IMPORT_RUNNING = "running"
IMPORT_SUCCEEDED = "succeeded"
IMPORT_FAILED = "failed"
IMPORT_CANCELLED = "cancelled"
IMPORT_CANCELLING = "cancelling"

# Progress phases are deliberately stable strings.  They cross the worker /
# render / Qt boundaries as plain metadata, so callers do not need to import
# the service's private worker implementation.
IMPORT_PHASE_QUEUED = "queued"
IMPORT_PHASE_PROBE = "probe"
IMPORT_PHASE_PARSE = "parse"
IMPORT_PHASE_HANDOFF = "handoff"
IMPORT_PHASE_DONE = "done"
IMPORT_PHASE_FAILED = "failed"
IMPORT_PHASE_CANCELLED = "cancelled"


@dataclass(frozen=True)
class ModelImportRequest:
    """一次 CPU 模型解析请求的不可变描述。"""

    path: str
    normalize: bool = False
    flip_uv_v: bool = False
    resource_budget_bytes: Optional[int] = None

    @classmethod
    def from_path(cls, path: str, *, normalize: bool = False,
                  flip_uv_v: bool = False,
                  resource_budget_bytes: Optional[int] = None
                  ) -> "ModelImportRequest":
        """构造规范化绝对路径请求，避免 worker 的 cwd 语义漂移。"""
        normalized = os.path.abspath(os.path.expanduser(os.fspath(path)))
        budget = (None if resource_budget_bytes is None else
                  max(0, int(resource_budget_bytes)))
        return cls(normalized, bool(normalize), bool(flip_uv_v), budget)


@dataclass(frozen=True)
class ModelImportResourceEstimate:
    """导入资源预估；只依赖文件大小，不读取模型内容。

    这是一个保守的 preflight heuristic，而不是实际显存计量：``cpu_bytes``
    覆盖解析/临时数组，``gpu_bytes`` 覆盖上传后的几何与纹理粗略开销。
    调用方只有显式设置预算时才会用 ``over_budget`` 阻断导入。
    """

    source_bytes: int = 0
    cpu_bytes: int = 0
    gpu_bytes: int = 0
    total_bytes: int = 0
    budget_bytes: Optional[int] = None
    over_budget: bool = False
    confidence: str = "source_size_heuristic"

    def to_dict(self) -> dict:
        """返回可跨线程、Qt 信号和 JSON 边界传递的字典。"""
        return {
            "source_bytes": int(self.source_bytes),
            "cpu_bytes": int(self.cpu_bytes),
            "gpu_bytes": int(self.gpu_bytes),
            "total_bytes": int(self.total_bytes),
            "budget_bytes": (int(self.budget_bytes)
                              if self.budget_bytes is not None else None),
            "over_budget": bool(self.over_budget),
            "confidence": str(self.confidence),
        }


def estimate_import_resources(
        path: str, *, budget_bytes: Optional[int] = None
        ) -> ModelImportResourceEstimate:
    """按源文件大小生成稳定、无内容读取的导入资源预估。"""
    try:
        source_bytes = max(0, int(os.path.getsize(path)))
    except (OSError, TypeError, ValueError):
        source_bytes = 0
    # Compressed model files can expand substantially while parsing and
    # uploading. Keep the multipliers explicit so the policy can be tuned
    # without changing the payload schema.
    cpu_bytes = source_bytes * 4 + (16 * 1024 * 1024 if source_bytes else 0)
    gpu_bytes = source_bytes * 2 + (8 * 1024 * 1024 if source_bytes else 0)
    total_bytes = cpu_bytes + gpu_bytes
    budget = (None if budget_bytes is None else max(0, int(budget_bytes)))
    return ModelImportResourceEstimate(
        source_bytes=source_bytes,
        cpu_bytes=cpu_bytes,
        gpu_bytes=gpu_bytes,
        total_bytes=total_bytes,
        budget_bytes=budget,
        over_budget=bool(budget is not None and total_bytes > budget),
    )


@dataclass(frozen=True)
class ModelImportResult:
    """后台导入的终态结果，可安全跨线程传递。"""

    request: ModelImportRequest
    status: str
    root: Any = None
    error: str = ""
    probe: Optional[FormatImportProbe] = None
    elapsed_s: float = 0.0
    phase: str = IMPORT_PHASE_DONE
    progress: float = 1.0
    resource_estimate: Optional[ModelImportResourceEstimate] = None

    @property
    def succeeded(self) -> bool:
        return self.status == IMPORT_SUCCEEDED and self.root is not None

    @property
    def cancelled(self) -> bool:
        return self.status == IMPORT_CANCELLED


class _CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()


class _ProgressState:
    def __init__(self) -> None:
        self._value = 0.0
        self._phase = IMPORT_PHASE_QUEUED
        self._lock = threading.Lock()

    def set(self, value: float, phase: Optional[str] = None) -> None:
        with self._lock:
            self._value = max(0.0, min(1.0, float(value)))
            if phase:
                self._phase = str(phase)

    def get(self) -> float:
        with self._lock:
            return self._value

    def get_phase(self) -> str:
        with self._lock:
            return self._phase

    def snapshot(self) -> tuple[float, str]:
        with self._lock:
            return self._value, self._phase


class ModelImportHandle:
    """调用方持有的导入句柄。

    ``poll`` 不阻塞：返回 ``None`` 表示仍在运行，返回结果表示已进入终态。
    ``result`` 只在调用方明确需要等待时使用。
    """

    def __init__(self, request: ModelImportRequest,
                 token: _CancellationToken,
                 progress: _ProgressState,
                 future: Future[ModelImportResult],
                 resource_estimate: ModelImportResourceEstimate) -> None:
        self.request = request
        self._token = token
        self._progress = progress
        self._future = future
        self._resource_estimate = resource_estimate
        self._result_cache: Optional[ModelImportResult] = None
        self._result_lock = threading.Lock()

    @property
    def done(self) -> bool:
        return self._future.done()

    @property
    def progress(self) -> float:
        return self._progress.get()

    @property
    def phase(self) -> str:
        """当前导入阶段；读取不会阻塞 worker。"""
        return self._progress.get_phase()

    def progress_snapshot(self) -> dict:
        """返回可安全跨线程/Qt 信号边界传递的进度快照。"""
        value, phase = self._progress.snapshot()
        return {"progress": value, "phase": phase, "state": self.state}

    @property
    def resource_estimate(self) -> ModelImportResourceEstimate:
        """提交时生成的资源预估，不等待导入完成。"""
        return self._resource_estimate

    @property
    def state(self) -> str:
        if not self.done:
            return IMPORT_CANCELLING if self._token.is_cancelled() else IMPORT_RUNNING
        return self.result().status

    def cancel(self) -> bool:
        """请求取消，返回是否成功将排队中的 Future 直接移除。"""
        self._token.cancel()
        cancelled = self._future.cancel()
        if cancelled:
            self._progress.set(1.0, IMPORT_PHASE_CANCELLED)
        return cancelled

    def poll(self) -> Optional[ModelImportResult]:
        """非阻塞读取终态；导入未结束时返回 ``None``。"""
        return self.result() if self.done else None

    def add_done_callback(self, callback: Callable[["ModelImportHandle"], None]) -> None:
        """注册终态回调；回调运行在 worker/调度线程，不能直接碰 Qt/GL。"""
        if not callable(callback):
            raise TypeError("callback must be callable")

        def _notify(_future: Future[ModelImportResult]) -> None:
            callback(self)

        self._future.add_done_callback(_notify)

    def result(self, timeout: Optional[float] = None) -> ModelImportResult:
        """读取终态结果，必要时按调用方指定的 timeout 等待。"""
        with self._result_lock:
            if self._result_cache is not None:
                return self._result_cache
        try:
            result = self._future.result(timeout=timeout)
        except CancelledError:
            result = ModelImportResult(
                request=self.request, status=IMPORT_CANCELLED,
                error="导入任务在开始前已取消。",
                phase=IMPORT_PHASE_CANCELLED,
                progress=1.0,
                resource_estimate=self._resource_estimate)
        with self._result_lock:
            self._result_cache = result
        return result


class ModelImportService:
    """共享的后台模型导入线程池。

    Service 本身不依赖 Qt；GUI/controller 可将 ``progress`` 和 ``poll`` 通过
    QTimer/信号接回主线程，render node 则只在自己的 render thread 消费 root。
    """

    def __init__(self, max_workers: int = 2) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="topos-model-import",
        )
        self._handles: Set[ModelImportHandle] = set()
        self._lock = threading.Lock()
        self._closed = False

    def submit(self, request: ModelImportRequest) -> ModelImportHandle:
        """提交一个导入请求；不会在调用线程解析模型。"""
        if not isinstance(request, ModelImportRequest):
            raise TypeError("request must be ModelImportRequest")
        token = _CancellationToken()
        progress = _ProgressState()
        resource_estimate = estimate_import_resources(
            request.path, budget_bytes=request.resource_budget_bytes)
        with self._lock:
            if self._closed:
                raise RuntimeError("ModelImportService is closed")
            future = self._executor.submit(
                self._run, request, token, progress, resource_estimate)
            handle = ModelImportHandle(
                request, token, progress, future, resource_estimate)
            self._handles.add(handle)

        def _forget(_future: Future[ModelImportResult]) -> None:
            with self._lock:
                self._handles.discard(handle)

        # Do not retain completed handles (and their potentially large
        # SceneGraphRoot) in the service-wide bookkeeping set.
        future.add_done_callback(_forget)
        return handle

    def active_handles(self) -> List[ModelImportHandle]:
        """返回当前未进入终态的句柄快照。"""
        with self._lock:
            return [handle for handle in self._handles if not handle.done]

    def cancel_all(self) -> int:
        """取消全部排队/运行任务，返回发出取消请求的数量。"""
        handles = self.active_handles()
        for handle in handles:
            handle.cancel()
        return len(handles)

    def shutdown(self, wait: bool = False) -> None:
        """关闭线程池；``wait=False`` 不阻塞调用线程。

        AUD-9005：Python>=3.9 的 ThreadPoolExecutor worker 是非 daemon 线程，
        解释器退出时会逐个 join——在途的第三方解析（assimp CLI 转换最长
        120s）会把应用退出阻塞最长约两分钟。这里在 cancel 后先终止登记中
        的 assimp CLI 子进程，让 worker 立刻从第三方等待中返回。
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.cancel_all()
        try:
            from .model_loader import _terminate_active_cli_exports
            terminated = _terminate_active_cli_exports()
            if terminated:
                logger.info(
                    "ModelImportService.shutdown: 已终止 %d 个在途 assimp CLI 转换",
                    terminated)
        except Exception:
            logger.warning("终止在途 assimp CLI 转换失败", exc_info=True)
        self._executor.shutdown(wait=wait, cancel_futures=True)

    @staticmethod
    def _run(request: ModelImportRequest, token: _CancellationToken,
             progress: _ProgressState,
             resource_estimate: ModelImportResourceEstimate
             ) -> ModelImportResult:
        started = time.monotonic()
        progress.set(0.05, IMPORT_PHASE_PROBE)
        if token.is_cancelled():
            progress.set(1.0, IMPORT_PHASE_CANCELLED)
            return ModelImportResult(
                request=request, status=IMPORT_CANCELLED,
                error="导入任务已取消。", elapsed_s=time.monotonic() - started,
                phase=IMPORT_PHASE_CANCELLED,
                resource_estimate=resource_estimate)

        probe = probe_import(request.path, require_exists=True)
        if not probe.can_import:
            progress.set(1.0, IMPORT_PHASE_FAILED)
            return ModelImportResult(
                request=request, status=IMPORT_FAILED,
                error=f"{probe.reason_code}: {probe.reason}", probe=probe,
                elapsed_s=time.monotonic() - started,
                phase=IMPORT_PHASE_FAILED,
                resource_estimate=resource_estimate)

        if resource_estimate.over_budget:
            progress.set(1.0, IMPORT_PHASE_FAILED)
            return ModelImportResult(
                request=request, status=IMPORT_FAILED,
                error=("resource.budget_exceeded: 预计导入资源 "
                       f"{resource_estimate.total_bytes} bytes 超过预算 "
                       f"{resource_estimate.budget_bytes} bytes。"),
                probe=probe, elapsed_s=time.monotonic() - started,
                phase=IMPORT_PHASE_FAILED, resource_estimate=resource_estimate)

        progress.set(0.15, IMPORT_PHASE_PARSE)
        try:
            # Importing here is intentional: model_loader has no GL calls in its
            # parse path. Any GL upload remains owned by Model3D render thread.
            from .model_loader import _set_cancel_check, load_model
            # AUD-9005：把取消令牌接到 loader 的 assimp CLI 等待循环——取消/
            # shutdown 时能立刻终止在途子进程，而不是等满 120s 超时。
            _set_cancel_check(token.is_cancelled)
            try:
                root = load_model(
                    request.path, normalize=request.normalize,
                    flip_uv_v=request.flip_uv_v)
            finally:
                _set_cancel_check(None)
        except Exception as exc:
            if token.is_cancelled():
                # 取消引发的终止（如 assimp CLI 子进程被杀）按取消归类，
                # 不算导入失败。
                progress.set(1.0, IMPORT_PHASE_CANCELLED)
                return ModelImportResult(
                    request=request, status=IMPORT_CANCELLED,
                    error=f"导入已取消: {exc}", probe=probe,
                    elapsed_s=time.monotonic() - started,
                    phase=IMPORT_PHASE_CANCELLED,
                    resource_estimate=resource_estimate)
            progress.set(1.0, IMPORT_PHASE_FAILED)
            return ModelImportResult(
                request=request, status=IMPORT_FAILED,
                error=str(exc), probe=probe,
                elapsed_s=time.monotonic() - started,
                phase=IMPORT_PHASE_FAILED,
                resource_estimate=resource_estimate)

        progress.set(0.85, IMPORT_PHASE_HANDOFF)
        if token.is_cancelled():
            # Do not expose a successful root after cancellation. The local
            # reference is released when this worker returns.
            progress.set(1.0, IMPORT_PHASE_CANCELLED)
            return ModelImportResult(
                request=request, status=IMPORT_CANCELLED,
                error="导入完成，但结果在交接前被取消。", probe=probe,
                elapsed_s=time.monotonic() - started,
                phase=IMPORT_PHASE_CANCELLED,
                resource_estimate=resource_estimate)

        progress.set(1.0, IMPORT_PHASE_DONE)
        return ModelImportResult(
            request=request, status=IMPORT_SUCCEEDED, root=root,
            probe=probe, elapsed_s=time.monotonic() - started,
            phase=IMPORT_PHASE_DONE, resource_estimate=resource_estimate)


_shared_service: Optional[ModelImportService] = None
_shared_service_lock = threading.Lock()


def get_shared_model_import_service() -> ModelImportService:
    """获取进程级共享导入服务。"""
    global _shared_service
    with _shared_service_lock:
        if _shared_service is None:
            _shared_service = ModelImportService()
        return _shared_service


def reset_shared_model_import_service() -> None:
    """测试/应用退出时关闭并清除共享服务。"""
    global _shared_service
    with _shared_service_lock:
        service = _shared_service
        _shared_service = None
    if service is not None:
        service.shutdown(wait=False)
