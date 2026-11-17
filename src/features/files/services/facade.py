"""类型化异步 facade（C 阶段，总计划 C 表第 2/4 行）。

职责：把 C1 的同步领域服务放进**有界 worker**执行；结果携带
request_id/generation/revision 身份；同一位置的新请求使旧请求作废
（迟到结果丢弃，绝不覆盖新调用）。错误经 ``FacadeError`` 送达，不用
空结果冒充成功。

线程口径：回调在 worker 线程触发；Qt 调用方自行 marshal 到 GUI 线程
（D 阶段控制器接线时做，不在本层耦合 Qt）。
"""
from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from src.features.files.contracts import (
    FilesError,
    QueryRequest,
    QueryResult,
    identity_key,
)
from src.features.files.repository.catalog import FilesCatalog
from src.features.files.services.domain import (
    FilesCollectionsService,
    FilesMetadataService,
    FilesScanService,
    FilesTaskService,
)
from src.features.files.services.query import (
    FilesLibraryService,
    FilesQueryService,
)
from src.shared.plugins.services import ServiceResult

MAX_WORKERS = 2


@dataclass(frozen=True)
class FacadeOutcome:
    """一次异步调用的送达结果（成功或 FilesError，二者互斥）。"""

    request_id: str
    key: str
    generation: int
    result: Any = None
    error: FilesError | None = None
    dropped: bool = False  # True 仅出现在 drop 统计里，不送达回调

    @property
    def ok(self) -> bool:
        return self.error is None


Listener = Callable[[FacadeOutcome], None]


class FacadeError(Exception):
    """facade 自身错误（未知请求/已关闭）——与领域 FilesError 分开。"""


class FilesFacade:
    """UI → 领域服务的唯一异步边界。"""

    def __init__(self, catalog: FilesCatalog, *, max_workers: int = MAX_WORKERS,
                 query: FilesQueryService | None = None,
                 metadata: FilesMetadataService | None = None,
                 scan: FilesScanService | None = None,
                 tasks: FilesTaskService | None = None) -> None:
        self._catalog = catalog
        self.query = query or FilesQueryService(catalog)
        self.metadata = metadata or FilesMetadataService(catalog)
        self.scan = scan or FilesScanService(catalog)
        self.tasks = tasks or FilesTaskService(catalog)
        self.libraries = FilesLibraryService(catalog)
        self.collections = FilesCollectionsService(catalog)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="files-facade")
        self._lock = threading.Lock()
        self._generations: dict[str, int] = {}
        self._pending: dict[str, Future] = {}
        self._dropped = 0
        self._closed = False

    # ------------------------------------------------------------ 统计/生命周期

    @property
    def dropped_results(self) -> int:
        with self._lock:
            return self._dropped

    def shutdown(self) -> None:
        """取消未开始的任务并回收线程池；进行中的回调不再送达。"""
        with self._lock:
            self._closed = True
            pending = list(self._pending.values())
            self._pending.clear()
        for future in pending:
            future.cancel()
        self._executor.shutdown(wait=True)

    # ------------------------------------------------------------ 浏览/搜索

    def list_directory(self, request: QueryRequest, *, library_id: str,
                       listener: Listener) -> None:
        self._submit(
            key=identity_key(request.path), request_id=request.request_id,
            work=lambda: self.query.list_directory(request, library_id=library_id),
            listener=listener,
            wrap=lambda result: result,
        )

    def search(self, query: str, *, library_id: str, request_id: str,
               listener: Listener, limit: int = 200, offset: int = 0) -> None:
        self._submit(
            key=f"search:{library_id}", request_id=request_id,
            work=lambda: self.query.search(
                query, library_id=library_id, request_id=request_id,
                limit=limit, offset=offset),
            listener=listener,
            wrap=lambda result: result,
        )

    # ------------------------------------------------------------ 扫描/任务/元数据

    def scan_library(self, library_id: str, *, request_id: str,
                     listener: Listener) -> None:
        self._submit(
            # 扫描/元数据不做同键作废：迟到的编辑被丢弃会丢用户数据
            key=f"scan:{request_id}", request_id=request_id,
            work=lambda: self.scan.scan_library(library_id),
            listener=listener,
            wrap=lambda result: result,
        )

    def start_task(self, library_id: str, operation: str, targets: list[str], *,
                   request_id: str, listener: Listener,
                   options: dict[str, Any] | None = None,
                   policy: str = "error") -> None:
        self._submit(
            key=f"task:{request_id}", request_id=request_id,
            work=lambda: self.tasks.start_task(
                library_id, operation, targets, options=options, policy=policy),
            listener=listener,
            wrap=lambda result: result,
        )

    def patch_metadata(self, paths: list[str], patch: dict[str, Any], *,
                       library_id: str, request_id: str, listener: Listener,
                       tags_op: str = "replace") -> None:
        self._submit(
            key=f"meta:{request_id}", request_id=request_id,
            work=lambda: self.metadata.patch_metadata(
                paths, patch, library_id=library_id, tags_op=tags_op),
            listener=listener,
            wrap=lambda result: result,
        )

    # ------------------------------------------------------------ 调度核心

    def _submit(self, *, key: str, request_id: str, work: Callable[[], Any],
                listener: Listener, wrap: Callable[[Any], Any]) -> None:
        with self._lock:
            if self._closed:
                raise FacadeError("facade 已关闭")
            generation = self._generations.get(key, 0) + 1
            self._generations[key] = generation
            stale = self._pending.pop(key, None)
        if stale is not None:
            stale.cancel()  # 未开始即取消；已开始的靠代际丢弃

        def _run() -> Any:
            return wrap(work())

        future = self._executor.submit(_run)
        with self._lock:
            if self._closed:
                future.cancel()
                return
            # 同 key 又来了新请求：这个 future 完成时也只算过期
            self._pending[key] = future

        def _deliver(completed: Future) -> None:
            with self._lock:
                current = self._generations.get(key, 0)
                superseded = generation < current or self._closed
                if superseded:
                    self._dropped += 1
                    self._pending.pop(key, None)
                    return
                self._pending.pop(key, None)
            try:
                value = completed.result()
                outcome = FacadeOutcome(request_id=request_id, key=key,
                                        generation=generation, result=value)
            except FilesError as error:
                outcome = FacadeOutcome(request_id=request_id, key=key,
                                        generation=generation, error=error)
            except Exception as exc:  # 领域层漏网的异常也必须可诊断送达
                outcome = FacadeOutcome(
                    request_id=request_id, key=key, generation=generation,
                    error=FilesError(code="service_unavailable",
                                     message=f"{exc.__class__.__name__}: {exc}",
                                     retriable=True))
            listener(outcome)

        future.add_done_callback(_deliver)


def query_result_of(outcome: FacadeOutcome) -> QueryResult:
    """类型化取值：浏览/搜索结果的窄化访问（错误时抛原 FilesError）。"""
    if outcome.error is not None:
        raise outcome.error
    assert isinstance(outcome.result, QueryResult)
    return outcome.result


def service_result_of(outcome: FacadeOutcome) -> ServiceResult:
    if outcome.error is not None:
        raise outcome.error
    assert isinstance(outcome.result, ServiceResult)
    return outcome.result
