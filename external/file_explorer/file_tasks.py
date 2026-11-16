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

线程模型：任务在独立线程执行，状态由锁保护；HTTP 只读写快照，不做长阻塞。
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

TASK_OPERATIONS = ("mkdir", "rename", "copy", "move", "trash", "restore", "purge")
CONFLICT_POLICIES = ("error", "overwrite", "rename")
_TERMINAL = ("completed", "failed", "cancelled")


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


class FileTaskService:
    """进程内任务表 + 单线程执行器（每个任务一个线程，逐个文件检查取消）。"""

    MAX_HISTORY = 200

    def __init__(self, metadata_hook: Callable[[str, str], None] | None = None) -> None:
        self._tasks: dict[str, FileTask] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._metadata_hook = metadata_hook or (lambda old, new: None)

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
            self._tasks[task.job_id] = task
            self._order.append(task.job_id)
            self._trim_locked()
        thread = threading.Thread(
            target=self._run, args=(task, operation, options),
            name=f"files-task-{operation}", daemon=True)
        thread.start()
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
        while len(self._order) > self.MAX_HISTORY:
            oldest = self._order.pop(0)
            self._tasks.pop(oldest, None)

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
                try:
                    destination = handler(task.items[index], task.policy, options)
                    task.mark_item(index, status="done", destination=destination or task.items[index].source)
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

    # -- 各操作 -------------------------------------------------------------
    def _do_mkdir(self, item: FileTaskItem, policy: str, options: dict[str, Any]) -> str:
        destination = self._resolve_destination(item, policy)
        os.makedirs(destination, exist_ok=False)
        return destination

    def _do_rename(self, item: FileTaskItem, policy: str, options: dict[str, Any]) -> str:
        if not os.path.lexists(item.source):
            raise ValueError("源文件不存在")
        destination = self._resolve_destination(item, policy)
        os.rename(item.source, destination)
        self._metadata_hook(item.source, destination)
        return destination

    def _do_copy(self, item: FileTaskItem, policy: str, options: dict[str, Any]) -> str:
        if not os.path.lexists(item.source):
            raise ValueError("源文件不存在")
        if os.path.isdir(item.source) and not options.get("recursive", True):
            raise ValueError("复制目录需要 recursive=true")
        destination = self._resolve_destination(item, policy)
        if os.path.isdir(item.source):
            shutil.copytree(item.source, destination, dirs_exist_ok=(policy == "overwrite"))
        else:
            shutil.copy2(item.source, destination)
        return destination

    def _do_move(self, item: FileTaskItem, policy: str, options: dict[str, Any]) -> str:
        if not os.path.lexists(item.source):
            raise ValueError("源文件不存在")
        if item.destination and os.path.abspath(item.destination) == os.path.abspath(item.source):
            return item.source  # 原地移动 = 无操作，不算冲突
        destination = self._resolve_destination(item, policy)
        shutil.move(item.source, destination)
        self._metadata_hook(item.source, destination)
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
        self._metadata_hook("", destination)
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
