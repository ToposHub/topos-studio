"""B07 / B09：文件任务服务（mkdir / rename / copy / move / trash / restore / purge）。

设计约束（来自计划 §5）
-----------------------
* **任务身份**：一次提交 = 一个 job_id，逐文件结果、进度、可取消；
* **冲突策略**：``error`` / ``overwrite`` / ``rename`` 三选一，逐项判定，
  不静默覆盖用户文件；
* **不伪成功**：磁盘失败进 ``failures``，成功的进 ``successes``，
  部分成功单独表达；
* **路径变化传播**：改名/移动/删除/恢复都要同步 ``file_meta``（含后代）、
  ``file_tags``、目录缓存与缩略图，恢复不能只恢复文件名（B09）。

[FR-01/02/07 整改] 线程与写入安全：
* 每个任务不再各开无界 daemon 线程，统一提交到**有界**线程池，
  服务 ``shutdown(wait=True)`` 可等待在途任务（FR-07）；
* 目标路径在进程内有**独占预约**（claim registry），同目标并发任务
  直接失败而不是双双成功；预约后用 **O_EXCL / mkdir / os.replace** 等
  原子原语落盘——不依赖"先 exists 后 move"的 TOCTOU 检查（FR-01）；
* 文件写入一律"同目录临时文件 + ``os.replace`` 原子替换"，**不跟随
  目标符号链接**——覆盖的是链接本身，绝不波及链接指向的文件（FR-02）；
* 拷贝分块进行（默认 4MB/块），每块检查取消、累计字节进度；取消或
  失败时清理占位与临时文件，不留半成品伪成功（FR-07）；
* 历史淘汰只回收**已结束**任务，运行中的任务永不因超出历史上限而
  丢失查询记录（FR-07）。

HTTP 只读写快照，不做长阻塞。
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import stat
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator

logger = logging.getLogger(__name__)

TASK_OPERATIONS = ("mkdir", "rename", "copy", "move", "trash", "restore", "purge")
CONFLICT_POLICIES = ("error", "overwrite", "rename")
_TERMINAL = ("completed", "failed", "cancelled")
_RENAME_RETRY_LIMIT = 64


class TaskCancelledError(RuntimeError):
    """分块传输中途检测到取消：由执行器统一转为 cancelled，不算失败。"""


class _DestinationBusyError(RuntimeError):
    """目标路径正被本服务另一个任务占用（进程内预约命中，FR-01）。"""


@dataclass
class FileTaskItem:
    source: str
    destination: str = ""
    status: str = "pending"          # pending / running / done / failed / skipped
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "destination": self.destination,
                "status": self.status, "error": self.error}


@dataclass
class FileTask:
    job_id: str
    operation: str
    policy: str
    items: list[FileTaskItem]
    status: str = "queued"           # queued / running / completed / failed / cancelled
    stage: str = "queued"
    created_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    cancel_requested: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def total(self) -> int:
        return len(self.items)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            done = sum(1 for item in self.items if item.status in ("done", "failed", "skipped"))
            return {
                "job_id": self.job_id,
                "operation": self.operation,
                "policy": self.policy,
                "status": self.status,
                "stage": self.stage,
                "total": self.total,
                "completed": done,
                "cancellable": self.status not in _TERMINAL,
                "cancelled": self.status == "cancelled",
                "created_at": self.created_at,
                "finished_at": self.finished_at,
                "progress": {"bytes_done": self.bytes_done,
                             "bytes_total": self.bytes_total},
                "items": [item.to_dict() for item in self.items],
                "successes": [item.destination or item.source for item in self.items
                              if item.status == "done"],
                "failures": [{"source": item.source, "error": item.error}
                             for item in self.items if item.status == "failed"],
            }

    def mark_item(self, index: int, **fields: Any) -> None:
        with self._lock:
            item = self.items[index]
            for key, value in fields.items():
                setattr(item, key, value)

    # FR-07：字节级进度（拷贝/跨卷移动分块累计）
    bytes_done: int = field(default=0, repr=False)
    bytes_total: int = field(default=0, repr=False)

    def add_progress(self, nbytes: int) -> None:
        with self._lock:
            self.bytes_done += int(nbytes)

    def set_progress_total(self, nbytes: int) -> None:
        with self._lock:
            self.bytes_total = int(nbytes)


class _RunContext:
    """单个任务执行上下文：取消检查 + 进度上报（FR-07）。

    经 threading.local 传入 ``_do_*`` 处理器，保持处理器签名
    ``(item, policy, options)`` 不变（既有测试与宿主依赖该签名）。
    """

    __slots__ = ("task", "cancelled")

    def __init__(self, task: FileTask | None) -> None:
        self.task = task
        self.cancelled = False

    def check_cancel(self) -> None:
        if self.cancelled:
            raise TaskCancelledError("已取消")
        if self.task is not None and self.task.cancel_requested:
            raise TaskCancelledError("已取消")

    def add_bytes(self, nbytes: int) -> None:
        if self.task is not None and nbytes:
            self.task.add_progress(nbytes)


def _claim_key(path: str) -> str:
    """预约键：绝对路径 + NFC。大小写不敏感卷的极端异形路径由原子原语兜底。"""
    return unicodedata.normalize("NFC", os.path.abspath(path))


def _silent_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


class FileTaskService:
    """进程内任务表 + 有界执行池（FR-07：每个任务占一个池位，逐文件检查取消）。"""

    MAX_HISTORY = 200
    MAX_WORKERS = 4
    CHUNK_SIZE = 4 * 1024 * 1024

    def __init__(self, metadata_hook: Callable[[str, str], None] | None = None) -> None:
        self._tasks: dict[str, FileTask] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._metadata_hook = metadata_hook or (lambda old, new: None)
        # FR-01：目标独占预约表（abs 目标路径 → job_id）
        self._claims: dict[str, str] = {}
        self._pool = ThreadPoolExecutor(
            max_workers=self.MAX_WORKERS, thread_name_prefix="files-task")
        self._closed = False
        self._local = threading.local()
        self._chunk_size = self.CHUNK_SIZE

    # -- 生命周期（FR-07：统一关闭，可等待在途任务）--------------------------
    def shutdown(self, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=wait)

    def __del__(self) -> None:  # 测试里大量短命服务，避免泄漏池线程
        try:
            self._pool.shutdown(wait=False)
        except Exception:
            pass

    # -- 提交与查询 ---------------------------------------------------------
    def start(self, operation: str, targets: Iterable[str], *, policy: str = "error",
              options: dict[str, Any] | None = None) -> FileTask:
        operation = str(operation or "").strip().lower()
        if operation not in TASK_OPERATIONS:
            raise ValueError(f"未知文件任务：{operation}")
        policy = str(policy or "error").lower()
        if policy not in CONFLICT_POLICIES:
            raise ValueError(f"未知冲突策略：{policy}")
        options = dict(options or {})
        items = self._plan(operation, [str(t) for t in targets if t], options)
        task = FileTask(uuid.uuid4().hex, operation, policy, items)
        with self._lock:
            if self._closed:
                raise RuntimeError("任务服务已关闭")
            self._tasks[task.job_id] = task
            self._order.append(task.job_id)
            self._trim_locked()
            submit_allowed = not self._closed
        if submit_allowed:
            self._pool.submit(self._run, task, operation, options)
        return task

    def status(self, job_id: str) -> dict[str, Any] | None:
        task = self._get(job_id)
        return task.snapshot() if task is not None else None

    def cancel(self, job_id: str) -> bool:
        task = self._get(job_id)
        if task is None:
            return False
        with task._lock:
            if task.status in _TERMINAL:
                return False
            task.cancel_requested = True
        return True

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            tasks = [self._tasks[job] for job in self._order if job in self._tasks]
        return [task.snapshot() for task in tasks]

    def _get(self, job_id: str) -> FileTask | None:
        with self._lock:
            return self._tasks.get(str(job_id))

    def _trim_locked(self) -> None:
        """FR-07：历史上限只淘汰已结束任务；运行中的任务永不丢失。"""
        finished = [job for job in self._order
                    if self._tasks.get(job) is not None
                    and self._tasks[job].status in _TERMINAL]
        excess = len(finished) - self.MAX_HISTORY
        for job in finished[:max(excess, 0)]:
            self._order.remove(job)
            self._tasks.pop(job, None)

    # -- 目标预约（FR-01）---------------------------------------------------
    @contextmanager
    def _claim_destination(self, destination: str, job_id: str) -> Iterator[None]:
        key = _claim_key(destination)
        with self._lock:
            if key in self._claims:
                raise _DestinationBusyError("目标正被其他任务占用，请稍后重试")
            self._claims[key] = job_id
        try:
            yield
        finally:
            with self._lock:
                if self._claims.get(key) == job_id:
                    del self._claims[key]

    # -- 计划 ---------------------------------------------------------------
    def _plan(self, operation: str, targets: list[str], options: dict[str, Any]) -> list[FileTaskItem]:
        if operation == "mkdir":
            parent = unicodedata.normalize("NFC", options.get("parent") or (targets[0] if targets else ""))
            name = str(options.get("name") or "").strip()
            if not parent or not name:
                return []
            if os.path.basename(name) != name or name in (".", ".."):
                return [FileTaskItem(parent, "", "failed", "非法文件夹名")]
            return [FileTaskItem(parent, os.path.join(parent, name))]
        if operation == "rename":
            source = unicodedata.normalize("NFC", targets[0]) if targets else ""
            name = str(options.get("new_name") or "").strip()
            if not source or not name:
                return []
            if os.path.basename(name) != name or name in (".", ".."):
                return [FileTaskItem(source, "", "failed", "非法文件名")]
            return [FileTaskItem(source, os.path.join(os.path.dirname(source), name))]
        if operation in ("copy", "move"):
            destination = unicodedata.normalize("NFC", options.get("destination") or "")
            return [FileTaskItem(source, os.path.join(destination, os.path.basename(source))
                                 if destination else "")
                    for source in targets]
        return [FileTaskItem(source) for source in targets]

    # -- 执行 ---------------------------------------------------------------
    def _run(self, task: FileTask, operation: str, options: dict[str, Any]) -> None:
        self._measure_progress_total(task)  # 先算总量（内部自行加锁，勿在 task._lock 内调用）
        with task._lock:
            task.status = "running"
            task.stage = operation
        if not task.items or any(item.status == "failed" for item in task.items):
            with task._lock:
                task.status = "failed"
                task.stage = "invalid"
                task.finished_at = time.time()
            return
        try:
            handler = getattr(self, f"_do_{operation}")
            context = _RunContext(task)
            for index in range(len(task.items)):
                with task._lock:
                    if task.cancel_requested:
                        task.status = "cancelled"
                        for pending in task.items:
                            if pending.status == "pending":
                                pending.status = "skipped"
                                pending.error = "已取消"
                        task.stage = "cancelled"
                        task.finished_at = time.time()
                        return
                    task.items[index].status = "running"
                self._local.ctx = context
                try:
                    destination = handler(task.items[index], task.policy, options)
                    task.mark_item(index, status="done", destination=destination or task.items[index].source)
                except TaskCancelledError:
                    # FR-07：分块传输中取消——当前项按“跳过”处理，不算失败
                    with task._lock:
                        task.status = "cancelled"
                        task.items[index].status = "skipped"
                        task.items[index].error = "已取消"
                        for pending in task.items[index + 1:]:
                            if pending.status == "pending":
                                pending.status = "skipped"
                                pending.error = "已取消"
                        task.stage = "cancelled"
                        task.finished_at = time.time()
                    return
                except Exception as exc:  # 逐项失败：继续处理其余文件
                    logger.error("file task %s failed on %s: %s", operation, task.items[index].source, exc)
                    task.mark_item(index, status="failed", error=str(exc) or exc.__class__.__name__)
            with task._lock:
                failed = any(item.status == "failed" for item in task.items)
                task.status = "failed" if failed else "completed"
                task.stage = "completed" if not failed else "completed-with-failures"
                task.finished_at = time.time()
        except Exception as exc:
            logger.exception("file task %s crashed", operation)
            with task._lock:
                task.status = "failed"
                task.stage = f"crashed: {exc}"
                task.finished_at = time.time()
        finally:
            self._local.ctx = None

    def _measure_progress_total(self, task: FileTask) -> None:
        """FR-07：普通文件任务给出可预知的字节总量（目录树保持 0=未知）。"""
        total = 0
        for item in task.items:
            try:
                if item.source and os.path.isfile(item.source) and not os.path.islink(item.source):
                    total += os.path.getsize(item.source)
            except OSError:
                pass
        if total:
            task.set_progress_total(total)

    def _ctx(self) -> _RunContext:
        ctx = getattr(self._local, "ctx", None)
        return ctx if ctx is not None else _RunContext(None)

    # -- 冲突处理 -----------------------------------------------------------
    @staticmethod
    def _resolve_destination(task_item: FileTaskItem, policy: str) -> str:
        destination = unicodedata.normalize("NFC", task_item.destination)
        if not destination:
            raise ValueError("目标路径为空")
        same_source = os.path.abspath(destination) == os.path.abspath(task_item.source)
        if same_source and policy == "overwrite":
            raise ValueError("目标与源相同，拒绝覆盖自身")
        if not os.path.lexists(destination):
            return destination
        # 大小写不敏感卷上仅改大小写：视为同一文件，放行（路径本身不同才允）
        if not same_source:
            try:
                if os.path.samefile(task_item.source, destination):
                    return destination
            except OSError:
                pass
        if policy == "overwrite":
            if os.path.isdir(destination) and not os.path.islink(destination):
                raise ValueError("目标已存在同名文件夹，拒绝覆盖")
            return destination
        if policy == "rename":
            return _unique_path(destination)
        raise ValueError("目标已存在")

    # -- 传输核心（FR-01/02/07）----------------------------------------------
    def _transfer(self, item: FileTaskItem, policy: str,
                  payload: Callable[[str, str, str, _RunContext], str]) -> str:
        """解析冲突 → 预约**实际写入路径** → 原子落盘。

        * 预约命中 → 立即失败（同目标并发任务，FR-01）。预约的是解析后的
          最终路径，rename 策略不会误占最终不会写入的原始目标名；
        * 预约后仍遇 FileExistsError（跨进程/极小窗口，resolve 的
          exists 检查已过期）→ error 报“目标已存在”，rename 策略换
          下一个唯一名重试。真正的防线是 O_EXCL/mkdir/os.replace。
        """
        candidate = item.destination
        attempts = 0
        while True:
            resolved = self._resolve_destination(
                FileTaskItem(item.source, candidate), policy)
            try:
                with self._claim_destination(resolved, self._current_job_id(item)):
                    return payload(item.source, resolved, policy, self._ctx())
            except _DestinationBusyError:
                # rename 语义 = "存在就换名"：并发占位等同即将存在，换名重试；
                # error/overwrite 不换名，如实报告占用冲突
                if policy == "rename" and attempts < _RENAME_RETRY_LIMIT:
                    attempts += 1
                    candidate = _unique_path(resolved)
                    continue
                raise
            except FileExistsError:
                if policy == "rename" and attempts < _RENAME_RETRY_LIMIT:
                    attempts += 1
                    candidate = _unique_path(resolved)
                    continue
                raise ValueError("目标已存在") from None

    @staticmethod
    def _current_job_id(item: FileTaskItem) -> str:
        return f"item:{_claim_key(item.source)}"

    def _copy_stream(self, source: str, fd: int, ctx: _RunContext) -> None:
        """分块拷贝：每块检查取消、累计进度（FR-07）。"""
        with open(source, "rb", buffering=0) as src:
            while True:
                ctx.check_cancel()
                chunk = src.read(self._chunk_size)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                ctx.add_bytes(len(chunk))

    def _write_mode(self, source: str) -> int:
        try:
            return stat.S_IMODE(os.stat(source, follow_symlinks=False).st_mode) or 0o644
        except OSError:
            return 0o644

    def _copy_file_payload(self, source: str, destination: str, policy: str,
                           ctx: _RunContext) -> str:
        """单文件拷贝（FR-01/02/07）。

        * overwrite：写同目录隐藏临时文件，完成后 os.replace 原子替换。
          os.replace 替换的是**目录项本身**——目标若为符号链接只会换掉
          链接，绝不跟随链接覆盖其指向的文件（FR-02）。
        * error/rename：O_EXCL 独占创建目标。rename 的解析结果应是全新
          唯一名；若竞态窗口里目标已被占用（FileExistsError），由
          _transfer 换下一个唯一名重试——rename 语义永不覆盖既有文件。
        """
        mode = self._write_mode(source)
        if policy != "overwrite":
            fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
            try:
                self._copy_stream(source, fd, ctx)
            except BaseException:
                os.close(fd)
                _silent_unlink(destination)  # 只清理自己刚独占创建的占位
                raise
            os.close(fd)
            shutil.copystat(source, destination)
            return destination
        tmp = os.path.join(
            os.path.dirname(destination) or ".",
            f".{os.path.basename(destination)}.{uuid.uuid4().hex}.topos-tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
        try:
            self._copy_stream(source, fd, ctx)
        except BaseException:
            os.close(fd)
            _silent_unlink(tmp)
            raise
        os.close(fd)
        shutil.copystat(source, tmp)
        os.replace(tmp, destination)
        return destination

    def _copy_entry(self, source: str, destination: str, overwrite: bool,
                    ctx: _RunContext) -> None:
        """树内逐项拷贝：符号链接只复制链接本身，普通文件走安全拷贝。"""
        if os.path.islink(source):
            if os.path.lexists(destination):
                if not overwrite:
                    raise ValueError("目标已存在")
                _silent_unlink(destination)
            os.symlink(os.readlink(source), destination)
            return
        if os.path.isdir(source):
            if overwrite:
                os.makedirs(destination, exist_ok=True)
            else:
                os.mkdir(destination)
            for name in sorted(os.listdir(source)):
                self._copy_entry(os.path.join(source, name),
                                 os.path.join(destination, name), overwrite, ctx)
            return
        self._copy_file_payload(source, destination,
                                "overwrite" if overwrite else "error", ctx)

    def _copy_dir_payload(self, source: str, destination: str, policy: str,
                          ctx: _RunContext) -> str:
        """目录拷贝：mkdir 原子占位（error/rename 不覆盖既有目录），再逐项拷入。"""
        if os.path.lexists(destination):
            raise FileExistsError(destination)
        os.mkdir(destination)
        try:
            for name in sorted(os.listdir(source)):
                self._copy_entry(os.path.join(source, name),
                                 os.path.join(destination, name), False, ctx)
        except BaseException:
            try:
                if os.path.isdir(destination) and not os.listdir(destination):
                    os.rmdir(destination)
            except OSError:
                pass
            raise
        return destination

    def _move_file_payload(self, source: str, destination: str, policy: str,
                           ctx: _RunContext) -> str:
        """单文件移动（FR-01）。

        * overwrite：os.replace 原子替换（不跟随目标符号链接）；
        * error/rename：O_EXCL 独占占位锁死目标 → 同卷 os.rename 原子
          替换自己的占位；跨卷（EXDEV）分块拷入占位后删源。任何失败都
          不触碰既有文件。
        """
        if policy == "overwrite":
            os.replace(source, destination)
            return destination
        mode = self._write_mode(source)
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
        os.close(fd)
        reserved = True
        try:
            try:
                os.rename(source, destination)
                reserved = False
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                wfd = os.open(destination, os.O_WRONLY)
                try:
                    self._copy_stream(source, wfd, ctx)
                finally:
                    os.close(wfd)
                shutil.copystat(source, destination)
                os.unlink(source)
                reserved = False
        except BaseException:
            if reserved:
                _silent_unlink(destination)  # 仅清自己的空占位，不动真实数据
            raise
        return destination

    def _move_dir_payload(self, source: str, destination: str, policy: str,
                          ctx: _RunContext) -> str:
        """目录移动：mkdir 原子占位 → 同卷 rename 替换空占位；跨卷拷树删源。"""
        if os.path.lexists(destination):
            raise FileExistsError(destination)
        os.mkdir(destination)
        reserved = True
        try:
            try:
                os.rename(source, destination)
                reserved = False
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                for name in sorted(os.listdir(source)):
                    self._copy_entry(os.path.join(source, name),
                                     os.path.join(destination, name), False, ctx)
                shutil.rmtree(source)
                reserved = False
        except BaseException:
            if reserved:
                try:
                    if os.path.isdir(destination) and not os.listdir(destination):
                        os.rmdir(destination)
                except OSError:
                    pass
            raise
        return destination

    def _notify_moved(self, source: str, destination: str) -> None:
        """文件已移动后的元数据平移；失败必须反馈（FR-03），不能静默吞掉。"""
        try:
            self._metadata_hook(source, destination)
        except Exception as exc:
            raise ValueError(
                f"文件已移动到 {destination}，但元数据同步失败：{exc}") from exc

    # -- 各操作 -------------------------------------------------------------
    def _do_mkdir(self, item: FileTaskItem, policy: str, options: dict[str, Any]) -> str:
        return self._transfer(item, policy,
                              lambda s, d, p, ctx: (os.makedirs(d, exist_ok=False), d)[1])

    def _do_rename(self, item: FileTaskItem, policy: str, options: dict[str, Any]) -> str:
        if not os.path.lexists(item.source):
            raise ValueError("源文件不存在")
        destination = self._transfer(item, policy, self._move_file_payload)
        self._notify_moved(item.source, destination)
        return destination

    def _do_copy(self, item: FileTaskItem, policy: str, options: dict[str, Any]) -> str:
        if not os.path.lexists(item.source):
            raise ValueError("源文件不存在")
        if os.path.isdir(item.source) and not options.get("recursive", True):
            raise ValueError("复制目录需要 recursive=true")
        if os.path.isdir(item.source) and not os.path.islink(item.source):
            return self._transfer(item, policy, self._copy_dir_payload)
        return self._transfer(item, policy, self._copy_file_payload)

    def _do_move(self, item: FileTaskItem, policy: str, options: dict[str, Any]) -> str:
        if not os.path.lexists(item.source):
            raise ValueError("源文件不存在")
        if item.destination and os.path.abspath(item.destination) == os.path.abspath(item.source):
            return item.source  # 原地移动 = 无操作，不算冲突
        if os.path.isdir(item.source) and not os.path.islink(item.source):
            destination = self._transfer(item, policy, self._move_dir_payload)
        else:
            destination = self._transfer(item, policy, self._move_file_payload)
        self._notify_moved(item.source, destination)
        return destination

    def _do_trash(self, item: FileTaskItem, policy: str, options: dict[str, Any]) -> str:
        import trash
        records, errors = trash.trash_items([item.source])
        if errors:
            raise ValueError("；".join(errors[:3]))
        if not records:
            raise ValueError("未产生回收站记录")
        return records[0].get("path", item.source)

    def _do_restore(self, item: FileTaskItem, policy: str, options: dict[str, Any]) -> str:
        import trash
        record_id = _trash_record_id(item.source)
        restored, errors = trash.restore_items([record_id])
        if errors:
            raise ValueError("；".join(errors[:3]))
        if not restored:
            raise ValueError("未恢复任何记录")
        record = restored[0] if isinstance(restored[0], dict) else {"path": item.source}
        destination = str(record.get("path") or item.source)
        return destination

    def _do_purge(self, item: FileTaskItem, policy: str, options: dict[str, Any]) -> str:
        import trash
        record_id = _trash_record_id(item.source)
        purged, errors = trash.purge_items([record_id])
        if errors:
            raise ValueError("；".join(errors[:3]))
        if not purged:
            raise ValueError("未清理任何记录")
        return item.source


def _trash_record_id(target: str) -> int:
    """接受回收站 id 或原路径：UI 两种入口都可能传，不能要求它记住 id。"""
    text = str(target or "").strip()
    if text.isdigit():
        return int(text)
    key = unicodedata.normalize("NFC", text)
    try:
        import trash
        for item in trash.list_items():
            if unicodedata.normalize("NFC", str(item.get("original_path") or "")) == key:
                return int(item["id"])
    except Exception as exc:
        raise ValueError(f"无法定位回收站记录：{exc}") from exc
    raise ValueError("回收站中没有该路径的记录")


def _unique_path(path: str) -> str:
    """``name.ext`` → ``name (1).ext``，直到不冲突；不做无界循环。"""
    base, extension = os.path.splitext(path)
    for index in range(1, 1000):
        candidate = f"{base} ({index}){extension}"
        if not os.path.lexists(candidate):
            return candidate
    raise ValueError("无法生成不冲突的新名称")


_service: FileTaskService | None = None
_service_lock = threading.Lock()


def get_service(metadata_hook: Callable[[str, str], None] | None = None) -> FileTaskService:
    global _service
    with _service_lock:
        if _service is None:
            _service = FileTaskService(metadata_hook)
        elif metadata_hook is not None:
            _service._metadata_hook = metadata_hook
        return _service
