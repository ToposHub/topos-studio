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
from pathlib import Path
from typing import Any, Callable

from src.features.files.contracts import (
    FilesError,
    FilesErrorCode,
    QueryRequest,
    QueryResult,
    identity_key,
    not_found_error,
    permission_error,
    unsupported_error,
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
from src.features.files.transactions import CancelToken, FileOperationService
from src.features.files.transactions.batch_plan import BatchPlan, BatchRenamePlanner, PlanError
from src.features.files.transactions.journal import OperationJournal
from src.shared.plugins.services import ItemResult, ServiceResult

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


def _plan_error(exc: PlanError) -> FilesError:
    """批改名计划错误 → 结构化 FilesError（details 携带计划错误码）。"""
    return FilesError(
        FilesErrorCode.INVALID_PAYLOAD, str(exc), False,
        details={"plan_error": exc.code})


class FilesFacade:
    """UI → 领域服务的唯一异步边界。"""

    def __init__(self, catalog: FilesCatalog, *, max_workers: int = MAX_WORKERS,
                 max_pending: int = 64,
                 query: FilesQueryService | None = None,
                 metadata: FilesMetadataService | None = None,
                 scan: FilesScanService | None = None,
                 tasks: FilesTaskService | None = None,
                 journal: OperationJournal | None = None) -> None:
        self._catalog = catalog
        # B3 生产接线：崩溃可恢复的写路径必须有持久 job_log（None=纯内存
        # 运行仅用于测试注入）。任务与批改名共用同一 journal。
        self.journal = journal if journal is not None else OperationJournal(catalog)
        self.query = query or FilesQueryService(catalog)
        self.metadata = metadata or FilesMetadataService(catalog)
        self.scan = scan or FilesScanService(catalog)
        self.tasks = tasks or FilesTaskService(
            catalog, journal=self.journal)
        self.libraries = FilesLibraryService(catalog)
        self.collections = FilesCollectionsService(catalog)
        self._planner = BatchRenamePlanner(
            catalog, service=FileOperationService(catalog, journal=self.journal))
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="files-facade")
        if max_pending < max_workers or max_workers < 1:
            raise ValueError("max_pending 必须不小于正数 max_workers")
        self._lock = threading.RLock()
        self._max_pending = max_pending
        self._all_pending: set[Future] = set()
        self._idle_callbacks: list[Callable[[], None]] = []
        self._sequence = 0
        self._pending: dict[str, Future] = {}
        self._dropped = 0
        self._closed = False

    # ------------------------------------------------------------ 统计/生命周期

    @property
    def dropped_results(self) -> int:
        with self._lock:
            return self._dropped

    @property
    def revision(self) -> int:
        """[F3] 当前素材库 revision（命令复核的库代数事实）。"""
        return self._catalog.revision

    def shutdown(self, *, wait: bool = False) -> None:
        """取消未开始的任务并回收线程池；进行中的回调不再送达。"""
        with self._lock:
            self._closed = True
            pending = list(self._all_pending)
            self._pending.clear()
        self.tasks.cancel_all()
        for future in pending:
            future.cancel()
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def when_idle(self, callback: Callable[[], None]) -> None:
        """Release owned resources only after every running/stale job completes."""
        with self._lock:
            if self._all_pending:
                self._idle_callbacks.append(callback)
                return
        callback()

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

    def tree(self, path: str, *, request_id: str, listener: Listener) -> None:
        """目录树子节点；同路径新请求作废旧请求。"""
        self._submit(
            key=f"tree:{identity_key(path)}", request_id=request_id,
            work=lambda: self.query.tree(path),
            listener=listener,
            wrap=lambda result: result,
        )

    def read_file(self, path: str, *, request_id: str, listener: Listener,
                  max_bytes: int) -> None:
        """同进程本地直读（预览/比较取字节）；同路径新请求作废旧读取。

        超限/缺失/无权限以 FilesError 送达，不部分传输。
        """
        def _work() -> bytes:
            return read_file_bytes(path, max_bytes)

        self._submit(
            key=f"read:{identity_key(path)}", request_id=request_id,
            work=_work,
            listener=listener,
            wrap=lambda result: result,
        )

    def collection_members(self, collection_id: int, *,
                           request_id: str, listener: Listener) -> None:
        """集合成员 → 目录条目（归属库内部解析；成员路径 + 磁盘真值水合）。"""
        def _work() -> QueryResult:
            library_id = self.collections.library_of(collection_id)
            return self.query.entries_for_paths(
                self.collections.member_paths(collection_id),
                request_id=request_id,
                path_label=f"collection://{collection_id}",
                library_id=library_id)

        self._submit(
            key="collection", request_id=request_id,
            work=_work, listener=listener,
            wrap=lambda result: result,
        )

    def batch_rename_preview(self, library_id: str, sources: list[str],
                             mode: str, options: dict[str, Any], *,
                             request_id: str, listener: Listener) -> None:
        """B6 两段式协议：预览返回计划（含冲突标记），不落盘。"""
        def _work() -> BatchPlan:
            try:
                return self._planner.preview(library_id, sources, mode, options)
            except PlanError as exc:
                raise _plan_error(exc) from exc

        self._submit(
            key=f"rename:{request_id}", request_id=request_id,
            work=_work, listener=listener,
            wrap=lambda result: result,
        )

    def batch_rename_execute(self, plan_id: str, library_id: str, *,
                             request_id: str, listener: Listener) -> None:
        def _work() -> Any:
            try:
                return self._planner.execute(plan_id, library_id)
            except PlanError as exc:
                raise _plan_error(exc) from exc

        self._submit(
            key=f"rename:{request_id}", request_id=request_id,
            work=_work, listener=listener,
            wrap=lambda result: result,
        )

    def scan_libraries(self, library_ids: list[str], *, request_id: str,
                       listener: Listener,
                       cancel: CancelToken | None = None,
                       progress: Callable[[int, int], None] | None = None) -> None:
        """顺序扫描多个库；进度经回调转发（worker 线程触发）。"""
        def _work() -> ServiceResult:
            aggregated: list[ItemResult] = []
            for library_id in library_ids:
                result = self.scan.scan_library(
                    library_id, cancel=cancel, progress=progress)
                aggregated.extend(result.items)
                if not result.ok:
                    return ServiceResult(
                        command_id="files.scan.all", ok=False,
                        accepted=True, stage=result.stage,
                        items=tuple(aggregated),
                        revision=result.revision, message=result.message)
            return ServiceResult(
                command_id="files.scan.all", ok=True, accepted=True,
                stage="completed", items=tuple(aggregated),
                revision=self._catalog.revision)

        self._submit(
            key=f"scan:{request_id}", request_id=request_id,
            work=_work, listener=listener,
            wrap=lambda result: result,
        )

    def run(self, key: str, request_id: str, work: Callable[[], Any],
            listener: Listener) -> None:
        """通用有界提交：轻量编排（库注册表等）复用同一 worker/身份语义。"""
        self._submit(key=key, request_id=request_id, work=work,
                     listener=listener, wrap=lambda result: result)

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
        def _run() -> Any:
            return wrap(work())

        with self._lock:
            if self._closed:
                raise FacadeError("facade 已关闭")
            saturated = len(self._all_pending) >= self._max_pending
            if not saturated:
                self._sequence += 1
                generation = self._sequence
                stale = self._pending.pop(key, None)
                if stale is not None:
                    stale.cancel()
                future = self._executor.submit(_run)
                self._all_pending.add(future)
                self._pending[key] = future
        if saturated:
            listener(FacadeOutcome(request_id=request_id, key=key, generation=0,
                                   error=FilesError(code="busy",message="Files 任务队列已满，请稍后重试",retriable=True)))
            return

        def _deliver_result(completed: Future) -> None:
            with self._lock:
                if self._closed or self._pending.get(key) is not completed:
                    self._dropped += 1
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

        def _deliver(completed: Future) -> None:
            try:
                _deliver_result(completed)
            finally:
                with self._lock:
                    self._all_pending.discard(completed)
                    callbacks = self._idle_callbacks if not self._all_pending else []
                    if callbacks:
                        self._idle_callbacks = []
                for callback in callbacks:
                    callback()

        future.add_done_callback(_deliver)


def _size_limit_label(max_bytes: int) -> str:
    if max_bytes >= 1024 * 1024:
        return f"{max_bytes // (1024 * 1024)}MB"
    return f"{max(max_bytes // 1024, 1)}KB"


def read_file_bytes(path: str, max_bytes: int) -> bytes:
    """同进程本地直读（预览/比较取字节）；超限/缺失/无权限以 FilesError 拒绝。"""
    target = Path(path)
    try:
        size = target.stat().st_size
    except FileNotFoundError as exc:
        raise not_found_error(path, "文件不存在或已被移动") from exc
    except PermissionError as exc:
        raise permission_error(path, "没有读取权限") from exc
    if size > max_bytes:
        raise unsupported_error(
            path, f"文件过大，超过 {_size_limit_label(max_bytes)} 预览上限")
    try:
        with open(target, "rb") as handle:
            data = handle.read(max_bytes + 1)
    except PermissionError as exc:
        raise permission_error(path, "没有读取权限") from exc
    if len(data) > max_bytes:  # 读取期间被追加：同样按超限拒绝
        raise unsupported_error(
            path, f"文件过大，超过 {_size_limit_label(max_bytes)} 预览上限")
    if not data:
        raise not_found_error(path, "该文件没有可预览内容")
    return data


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
