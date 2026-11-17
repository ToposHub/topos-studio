"""B08：外部文件投放的探测协调（异步、可取消、不持有 Qt 事件）。

背景
----
旧路径在 ``QDropEvent`` 处理函数里同步探测每个文件（多文件时 UI 阻塞
≈N×probe），并且逐个片段插入，导致"一次拖 N 个文件"不是一次原子撤销。

本模块只负责 **探测 / 去重 / 逐项结果** 这三件后台工作：
* 立即复制出路径与目标上下文，不把 ``QDropEvent`` 带进线程；
* 在受控线程池里只读探测，支持中途取消；
* 结果回到 GUI 线程后，由调用方（时间线视图）重新校验活动工程、
  revision、轨道锁，再用既有命令栈提交（单宏 = 单次 Undo）。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

from src.features.files.contracts import (
    FilesError,
    FilesErrorCode,
    JobResult,
    files_error_for_probe,
)


@dataclass(frozen=True)
class DropContext:
    """投放目标快照；提交前必须重新核对真实工程状态。"""

    job_id: int
    project_id: str
    project_revision: int
    sequence_revision: int
    track_id: str | None
    create_track: bool
    track_type: str
    frame: int
    fps: float = 0.0

    def describe(self) -> str:
        return (f"{self.project_id}@{self.project_revision}/seq{self.sequence_revision}"
                f"/{self.track_id or 'new-track'}/f{self.frame}")


@dataclass(frozen=True)
class ImportCandidate:
    path: str
    media_type: str
    is_new: bool
    metadata: dict[str, Any] = field(default_factory=dict)
    error: FilesError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class ImportProbeResult:
    job_id: int
    candidates: tuple[ImportCandidate, ...] = ()
    cancelled: bool = False

    @property
    def usable(self) -> tuple[ImportCandidate, ...]:
        return tuple(item for item in self.candidates if item.ok)

    @property
    def failures(self) -> tuple[ImportCandidate, ...]:
        return tuple(item for item in self.candidates if not item.ok)

    def summary(self) -> str:
        usable = len(self.usable)
        failed = len(self.failures)
        if self.cancelled and not usable:
            return "导入已取消"
        text = f"可导入 {usable}"
        if failed:
            text += f" · 失败 {failed}"
        if self.cancelled:
            text += " · 已取消"
        return text

    def to_job_result(self) -> JobResult:
        return JobResult(
            job_id=str(self.job_id),
            successes=tuple(item.path for item in self.usable),
            failures=tuple(
                # 逐项失败单独表达，避免"整批失败"掩盖部分成功
                _job_failure(item) for item in self.failures
            ),
            cancelled=self.cancelled,
        )


def _job_failure(candidate: ImportCandidate) -> Any:
    from src.features.files.contracts import JobFailure
    return JobFailure(candidate.path, candidate.error or FilesError(
        FilesErrorCode.UNKNOWN, "导入失败", True, candidate.path))


class _ProbeSignals(QObject):
    finished = pyqtSignal(int, object)


def _default_probe(path: str) -> dict[str, Any]:
    from src.features.edit.core.project import probe_media_metadata
    return probe_media_metadata(path) or {}


def _default_classify(path: str) -> str:
    from src.features.edit.core.project import get_media_type
    return get_media_type(path)


class ImportProbeJob(QRunnable):
    """一次投放的探测任务；``cancel()`` 后不再启动新的探测。"""

    def __init__(
        self,
        job_id: int,
        paths: Iterable[str],
        existing_paths: Iterable[str] = (),
        *,
        probe: Callable[[str], dict[str, Any]] | None = None,
        classify: Callable[[str], str] | None = None,
        max_workers: int = 4,
    ) -> None:
        super().__init__()
        self.job_id = job_id
        self.paths = [str(path) for path in paths]
        self._existing = {str(path) for path in existing_paths}
        self._probe = probe or _default_probe
        self._classify = classify or _default_classify
        self._max_workers = max(1, int(max_workers))
        self._cancel = threading.Event()
        self.signals = _ProbeSignals()
        self.setAutoDelete(False)

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self) -> None:
        results: list[ImportCandidate] = []
        try:
            if self._cancel.is_set():
                self.signals.finished.emit(self.job_id, ImportProbeResult(self.job_id, (), True))
                return
            with ThreadPoolExecutor(max_workers=min(self._max_workers, max(1, len(self.paths))),
                                    thread_name_prefix="files-import-probe") as pool:
                futures = {path: pool.submit(self._probe_one, path) for path in self.paths}
                for path, future in futures.items():
                    if self._cancel.is_set():
                        # 取消后不再收集：已完成的探测结果同样作废，避免插入半批
                        break
                    try:
                        results.append(future.result())
                    except Exception as exc:  # 探测实现抛错 = 该项失败，不中断整批
                        results.append(ImportCandidate(
                            path=path,
                            media_type="",
                            is_new=path not in self._existing,
                            error=files_error_for_probe(path, exc),
                        ))
        finally:
            self.signals.finished.emit(
                self.job_id, ImportProbeResult(self.job_id, tuple(results), self._cancel.is_set()))

    def _probe_one(self, path: str) -> ImportCandidate:
        if self._cancel.is_set():
            return ImportCandidate(path, "", path not in self._existing,
                                   error=FilesError(FilesErrorCode.CANCELLED, "已取消", True, path))
        try:
            media_type = self._classify(path)
        except Exception as exc:
            return ImportCandidate(path, "", path not in self._existing,
                                   error=files_error_for_probe(path, exc))
        try:
            metadata = self._probe(path) or {}
        except Exception as exc:
            return ImportCandidate(path, media_type, path not in self._existing,
                                   error=files_error_for_probe(path, exc))
        return ImportCandidate(path, media_type, path not in self._existing, metadata)


class ImportProbeRunner(QObject):
    """把 ``ImportProbeJob`` 投到线程池并保证结果回到调用方所在线程。"""

    finished = pyqtSignal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._job: ImportProbeJob | None = None
        # [E1/FUP-06] 已取消但仍在运行的任务：保留强引用直到真实完成，
        # 否则 setAutoDelete(False) 的 job 只剩 C++ 侧指针，Python 包装
        # 被 GC 后 run() 退化为抽象基类
        self._retired: list[ImportProbeJob] = []
        self._job_id = 0

    @property
    def job_id(self) -> int:
        return self._job_id

    def start(
        self,
        paths: Iterable[str],
        existing_paths: Iterable[str] = (),
        *,
        probe: Callable[[str], dict[str, Any]] | None = None,
        classify: Callable[[str], str] | None = None,
    ) -> int:
        self.cancel()
        self._job_id += 1
        job = ImportProbeJob(self._job_id, paths, existing_paths, probe=probe, classify=classify)
        job.signals.finished.connect(self._on_finished)
        self._job = job
        QThreadPool.globalInstance().start(job)
        return self._job_id

    def cancel(self) -> None:
        job, self._job = self._job, None
        if job is not None:
            job.cancel()
            # 仍在队列里的任务直接丢弃（tryTake 成功即回收）；已运行的靠
            # cancel 标志提前返回，引用保留到 finished 送达时回收
            if not QThreadPool.globalInstance().tryTake(job):
                self._retired.append(job)

    def _on_finished(self, job_id: int, result: ImportProbeResult) -> None:
        # 真实完成即回收退役引用（含被取消的旧任务：它们的 finished 同样会到）
        if self._retired:
            self._retired = [job for job in self._retired if job.job_id != job_id]
        if job_id != self._job_id:
            return
        self._job = None
        self.finished.emit(result)


@dataclass(frozen=True)
class ImportCommitPlan:
    """一次投放的导入结果：工程可能已被替换，失败项逐条保留。"""

    project: Any
    assets: tuple[Any, ...] = ()
    failures: tuple[Any, ...] = ()
    changed: bool = False


def prepare_import(
    project: Any,
    candidates: Iterable[ImportCandidate],
    *,
    import_asset: Callable[..., Any] | None = None,
    fallback_metadata: Callable[[str], dict[str, Any]] | None = None,
    classify: Callable[[str], str] | None = None,
) -> ImportCommitPlan:
    """按路径去重后导入：已存在素材复用，新文件逐项导入，失败不阻断整批。

    这是 B08 的领域逻辑（不碰 Qt、不碰视图状态），因此可单独测试：
    * 同一路径重复 → 复用同一个 asset，不重复建；
    * 单个文件探测/导入失败 → 只记入 failures；
    * ``import_asset`` 返回新工程时整体替换，不混用半旧半新对象。
    """
    from src.features.files.contracts import ImportFailureCandidate

    failures: list[Any] = []
    assets: list[Any] = []
    current = project
    changed = False
    importer = import_asset
    for candidate in candidates:
        if current is None:
            break
        if not candidate.ok:
            failures.append(candidate)
            continue
        try:
            existing = current.get_asset_by_path(candidate.path)
        except AttributeError:
            existing = None
        if existing is not None:
            assets.append(existing)
            continue
        try:
            metadata = candidate.metadata
            if metadata is None and fallback_metadata is not None:
                metadata = fallback_metadata(candidate.path)
            media_type = candidate.media_type or (
                classify(candidate.path) if classify is not None else "")
            if importer is None:
                new_project = current.import_asset(
                    path=candidate.path, media_type=media_type, metadata=metadata or {})
            else:
                new_project = importer(current, candidate, metadata or {}, media_type)
            asset = new_project.get_asset_by_path(candidate.path)
            if asset is None:
                raise RuntimeError("导入后未在工程中找到该素材")
        except Exception as exc:  # 逐项失败不阻断其余文件
            failures.append(ImportFailureCandidate(candidate.path, exc))
            continue
        if new_project is not current:
            current = new_project
            changed = True
        assets.append(asset)
    return ImportCommitPlan(current, tuple(assets), tuple(failures), changed)


def dedupe_paths(paths: Iterable[str]) -> list[str]:
    """同一次投放内的路径去重（NFC + 大小写不敏感，保留首次出现顺序）。"""
    from src.features.files.contracts import identity_key

    seen: dict[str, str] = {}
    for path in paths:
        text = str(path)
        if not text:
            continue
        key = identity_key(text)
        seen.setdefault(key, text)
    return list(seen.values())


def project_existing_paths(project: Any) -> list[str]:
    """工程内已有素材路径（用于 import 前去重，避免重复建 asset）。"""
    assets = getattr(project, "assets", {}) or {}
    paths: list[str] = []
    for asset in assets.values():
        path = getattr(asset, "path", None)
        if path:
            paths.append(str(path))
    return paths


def unsupported_extension_reason(path: str) -> str:
    return f"{Path(path).suffix or '无扩展名'} 不在可导入的媒体格式内"
