"""元数据/集合/扫描/文件任务领域服务（C1）。

统一返回 shared ``ServiceResult``（阶段词表 PL-07）；错误码与 B 阶段
结构化 code 一致。所有写路径经 ``FileOperationService``/catalog，
不自建第二条文件系统或组织数据通道。
"""
from __future__ import annotations

import os
import stat
import time
import secrets
import threading
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from src.features.files.contracts import split_tags
from src.features.files.repository.catalog import CatalogError, FilesCatalog, nfc
from src.features.files.transactions.ops import OpError
from src.features.files.transactions import (
    CancelToken,
    FileOperationService,
    ItemOutcome,
)
from src.features.files.transactions.journal import OperationJournal
from src.shared.plugins.services import (
    ItemResult,
    ServiceResult,
    accepted,
    rejected,
)

_USER_FIELDS = ("rating", "tags", "color", "flag", "comments")


class FilesMetadataService:
    """get/patch 用户字段；patch 只动给定字段（B1 语义），FTS 随触发器。"""

    def __init__(self, catalog: FilesCatalog) -> None:
        self._catalog = catalog

    def get_metadata(self, paths: Iterable[str], *,
                     library_id: str = "") -> ServiceResult:
        items: list[ItemResult] = []
        for raw in paths:
            path = nfc(raw)
            row = self._catalog.asset(library_id, path) if library_id else None
            if row is None:
                items.append(ItemResult(path, ok=False, error_code="NOT_FOUND",
                                        message="资产未登记（先扫描或固定库）"))
                continue
            items.append(ItemResult(
                path, ok=True,
                result={"rating": row["rating"], "color": row["color"],
                        "flag": row["flag"], "comments": row["comment"],
                        "tags": self._catalog.tags_of(library_id, path)}))
        return ServiceResult(
            command_id="files.metadata.get", ok=all(i.ok for i in items),
            accepted=True, stage="completed", items=tuple(items),
            revision=self._catalog.revision)

    def patch_metadata(self, paths: Iterable[str], patch: dict[str, Any], *,
                       library_id: str = "",
                       tags_op: str = "replace") -> ServiceResult:
        unknown = set(patch) - set(_USER_FIELDS)
        if unknown:
            return rejected("files.metadata.patch",
                            error_code="INVALID_PATCH",
                            message=f"未知字段: {sorted(unknown)}")
        if tags_op not in ("replace", "add", "remove", "clear"):
            return rejected("files.metadata.patch", error_code="INVALID_PATCH",
                            message=f"未知 tags_op: {tags_op}")
        for name, low, high in (("rating", 0, 5), ("color", 0, 9), ("flag", -1, 1)):
            if name in patch and (isinstance(patch[name], bool) or not isinstance(patch[name], int)
                                  or not low <= patch[name] <= high):
                return rejected("files.metadata.patch", error_code="INVALID_PATCH", message=f"{name} 超出合法范围")
        if "comments" in patch and not isinstance(patch["comments"], str):
            return rejected("files.metadata.patch",error_code="INVALID_PATCH",message="comments 必须是文本")
        items: list[ItemResult] = []
        for raw in paths:
            path = nfc(raw)
            row = self._catalog.asset(library_id, path)
            if row is None:
                items.append(ItemResult(path, ok=False, error_code="NOT_FOUND"))
                continue
            field_patch = {("comment" if k == "comments" else k): v for k, v in patch.items() if k != "tags"}
            with self._catalog._write():
                if "tags" in patch:
                    self._apply_tags(library_id, path, patch["tags"], tags_op)
                if field_patch:
                    self._catalog.patch_user_fields(library_id, path, **field_patch)
            final = self._catalog.asset(library_id, path) or {}
            items.append(ItemResult(path, ok=True, result={
                "rating": final.get("rating", 0),
                "color": final.get("color", 0),
                "flag": final.get("flag", 0),
                "comments": final.get("comment", ""),
                "tags": self._catalog.tags_of(library_id, path)}))
        return ServiceResult(
            command_id="files.metadata.patch",
            ok=all(i.ok for i in items), accepted=True, stage="completed",
            items=tuple(items), revision=self._catalog.revision)

    def _apply_tags(self, library_id: str, path: str,
                    tags: Any, op: str) -> None:
        # UI 语义是逗号分隔文本；列表/元组调用方也兼容
        desired = set(split_tags(tags))
        if op == "clear":
            desired = set()  # clear = 全量移除（UI 空输入即清空的显式语义）
        with self._catalog._read() as cursor:
            existing_rows = cursor.execute(
                "SELECT t.id, t.name FROM asset_tags at JOIN tags t"
                " ON t.id = at.tag_id WHERE at.library_id = ? AND at.path = ?",
                (library_id, path)).fetchall()
        existing = {row["name"]: row["id"] for row in existing_rows}
        if op in ("replace", "clear"):
            for name, tag_id in existing.items():
                if name not in desired:
                    self._catalog.untag_asset(library_id, path, tag_id)
        additions = desired - set(existing) if op != "remove" else set()
        for name in additions:
            with self._catalog._read() as cursor:
                row = cursor.execute(
                    "SELECT id FROM tags WHERE library_id = ? AND name = ?",
                    (library_id, name)).fetchone()
            tag_id = row["id"] if row is not None else self._catalog.create_tag(
                library_id, name)
            self._catalog.tag_asset(library_id, path, tag_id)
        if op == "remove":
            for name in desired & set(existing):
                self._catalog.untag_asset(library_id, path, existing[name])


class FilesCollectionsService:
    """集合 CRUD 与成员增删（数据归 catalog.collections）。"""

    def __init__(self, catalog: FilesCatalog) -> None:
        self._catalog = catalog

    def list_collections(self, library_id: str = "") -> ServiceResult:
        with self._catalog._read() as cursor:
            if library_id:
                rows = cursor.execute(
                    "SELECT c.id, c.name, c.color, c.library_id,"
                    " (SELECT COUNT(*) FROM collection_items ci"
                    "  WHERE ci.collection_id = c.id) AS item_count"
                    " FROM collections c WHERE c.library_id = ? ORDER BY c.name",
                    (library_id,)).fetchall()
            else:
                rows = cursor.execute(
                    "SELECT c.id, c.name, c.color, c.library_id,"
                    " (SELECT COUNT(*) FROM collection_items ci"
                    "  WHERE ci.collection_id = c.id) AS item_count"
                    " FROM collections c ORDER BY c.name").fetchall()
        return ServiceResult(
            command_id="files.collections.list", ok=True, accepted=True,
            stage="completed",
            items=tuple(ItemResult(str(row["id"]), ok=True, result={
                "id": row["id"], "name": row["name"], "color": row["color"],
                "library_id": row["library_id"],
                "item_count": row["item_count"]})
                for row in rows),
            revision=self._catalog.revision)

    def library_of(self, collection_id: int) -> str:
        """集合归属库（成员水合叠加用）；未知集合返回空串。"""
        with self._catalog._read() as cursor:
            row = cursor.execute(
                "SELECT library_id FROM collections WHERE id = ?",
                (int(collection_id),)).fetchone()
        return str(row["library_id"]) if row is not None else ""

    def create_collection(self, library_id: str, name: str) -> ServiceResult:
        if not name.strip():
            return rejected("files.collections.create", error_code="INVALID_NAME",
                            message="集合名不能为空")
        try:
            cid = self._catalog.create_collection(library_id, name.strip())
        except Exception as exc:  # UNIQUE 冲突等
            return rejected("files.collections.create", error_code="CONFLICT",
                            message=str(exc))
        return accepted("files.collections.create", stage="completed",
                        revision=self._catalog.revision,
                        message=str(cid))

    def add_members(self, collection_id: int, paths: Iterable[str]) -> ServiceResult:
        items = []
        for raw in paths:
            try:
                self._catalog.add_to_collection(collection_id, raw)
                items.append(ItemResult(nfc(raw), ok=True))
            except Exception as exc:
                items.append(ItemResult(nfc(raw), ok=False,
                                        error_code="CONFLICT", message=str(exc)))
        return ServiceResult(command_id="files.collections.add", ok=all(item.ok for item in items),
                             accepted=True, stage="completed", items=tuple(items),
                             revision=self._catalog.revision)

    def member_paths(self, collection_id: int) -> list[str]:
        """成员路径按入集顺序返回；水合（磁盘真值+叠加）归查询服务。"""
        return list(self._catalog.collection_member_paths(collection_id))

    def rename_collection(self, collection_id: int, name: str) -> ServiceResult:
        clean = str(name or "").strip()
        if not clean:
            return rejected("files.collections.rename", error_code="INVALID_NAME",
                            message="集合名不能为空")
        try:
            changed = self._catalog.rename_collection(collection_id, clean)
        except Exception as exc:  # 同库重名（UNIQUE）等
            return rejected("files.collections.rename", error_code="CONFLICT",
                            message=str(exc))
        if not changed:
            return rejected("files.collections.rename", error_code="NOT_FOUND",
                            message="集合不存在")
        return accepted("files.collections.rename", stage="completed",
                        revision=self._catalog.revision)

    def remove_members(self, collection_id: int,
                       paths: Iterable[str]) -> ServiceResult:
        items = []
        for raw in paths:
            removed = self._catalog.remove_from_collection(collection_id, raw)
            items.append(ItemResult(nfc(raw), ok=removed,
                                    error_code="" if removed else "NOT_FOUND"))
        return ServiceResult(command_id="files.collections.remove", ok=all(item.ok for item in items),
                             accepted=True, stage="completed", items=tuple(items),
                             revision=self._catalog.revision)

    def delete_collection(self, collection_id: int) -> ServiceResult:
        if not self._catalog.delete_collection(collection_id):
            return rejected("files.collections.delete", error_code="NOT_FOUND",
                            message="集合不存在")
        return accepted("files.collections.delete", stage="completed",
                        revision=self._catalog.revision)


class FilesScanService:
    """库扫描：登记资产技术列 + 标 missing；有界、可取消、带进度回调。"""

    def __init__(self, catalog: FilesCatalog) -> None:
        self._catalog = catalog

    def scan_library(self, library_id: str, *,
                     cancel: CancelToken | None = None,
                     progress: Callable[[int, int], None] | None = None,
                     chunk: int = 200) -> ServiceResult:
        record = next((lib for lib in self._catalog.libraries()
                       if lib.library_id == library_id), None)
        if record is None:
            return rejected("files.scan", error_code="LIBRARY_UNKNOWN",
                            message="库未注册")
        root = Path(record.root)
        if not root.is_dir():
            return rejected("files.scan", error_code="PATH_NOT_FOUND",
                            message="库根目录不存在", retriable=True)
        seen: set[str] = set()
        scanned = 0
        cancelled = False
        for current, dirs, files in os.walk(root):
            dirs[:] = [name for name in dirs if not name.startswith(".")]
            if cancel is not None and cancel.requested:
                cancelled = True
                break
            for file_name in files:
                if cancel is not None and cancel.requested:
                    cancelled = True
                    break
                if file_name.startswith("."):
                    continue
                raw_path = os.path.join(current, file_name)
                try:
                    info = os.lstat(raw_path)
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                path = nfc(raw_path)
                seen.add(path)
                self._catalog.upsert_asset(
                    library_id, path, size=info.st_size, mtime=info.st_mtime,
                    indexed_at=time.time())
                scanned += 1
                if progress is not None and scanned % chunk == 0:
                    progress(scanned, -1)
            if cancelled:
                break
        missing_marked = 0
        if not cancelled:
            missing_marked = self._mark_missing(library_id, seen)
        if progress is not None:
            progress(scanned, scanned)
        return ServiceResult(
            command_id="files.scan",
            ok=not cancelled, accepted=True,
            stage="cancelled" if cancelled else "completed",
            items=(ItemResult(str(root), ok=True,
                              result={"scanned": scanned,
                                      "missing_marked": missing_marked}),),
            revision=self._catalog.revision,
            message="扫描被取消（部分结果已入库，可重扫）" if cancelled else "")

    def _mark_missing(self, library_id: str, seen: set[str]) -> int:
        with self._catalog._read() as cursor:
            rows = cursor.execute(
                "SELECT path FROM assets WHERE library_id = ? AND missing = 0",
                (library_id,)).fetchall()
        missing = [row["path"] for row in rows if row["path"] not in seen]
        with self._catalog._write() as cursor:
            for path in missing:
                cursor.execute(
                    "UPDATE assets SET missing = 1 WHERE library_id = ? AND path = ?",
                    (library_id, path))
        return len(missing)


@dataclass
class _RunningTask:
    job_id: str
    cancel: CancelToken
    outcomes: list[ItemOutcome] = field(default_factory=list)


class FilesTaskService:
    """文件任务：mkdir/rename/copy/move/trash/restore/purge。

    C1 为同步执行（每条目间检查取消）；C2 facade 在有界 worker 中调用。
    任务身份/历史归 B3 journal；执行归 B2/B5 统一入口。
    """

    OPERATIONS = ("mkdir", "rename", "copy", "move", "trash", "restore", "purge")

    def __init__(self, catalog: FilesCatalog, *,
                 operations: FileOperationService | None = None,
                 journal: OperationJournal | None = None) -> None:
        self._catalog = catalog
        self._operations = operations or FileOperationService(
            catalog, journal=journal)
        self._running: dict[str, _RunningTask] = {}
        self._lock = threading.RLock()
        self._closed = False

    def start_task(self, library_id: str, operation: str,
                   targets: Iterable[str], *,
                   options: dict[str, Any] | None = None,
                   policy: str = "error",
                   job_id: str | None = None,
                   cancel: CancelToken | None = None,
                   progress: Callable[[int, int], None] | None = None) -> ServiceResult:
        if operation not in self.OPERATIONS:
            return rejected("files.task.start", error_code="INVALID_OPERATION",
                            message=f"未知操作 {operation}")
        targets = [nfc(t) for t in targets]
        if not targets and operation != "mkdir":
            return rejected("files.task.start", error_code="EMPTY_TARGETS",
                            message="没有目标")
        options = dict(options or {})
        cancel = cancel if cancel is not None else CancelToken()
        job_id = job_id or f"task-{secrets.token_hex(12)}"
        with self._lock:
            if self._closed or job_id in self._running:
                return rejected("files.task.start", error_code="TASK_REJECTED",
                                message="任务服务已关闭或任务 id 已在运行")
            self._running[job_id] = _RunningTask(job_id=job_id, cancel=cancel, outcomes=[])
        outcomes: list[ItemOutcome] = []
        try:
            for target in targets:
                if cancel.requested:
                    outcomes.append(ItemOutcome(target, status="cancelled", code="CANCELLED"))
                else:
                    try:
                        outcomes.append(self._run_one(
                            library_id, operation, target, options, policy, cancel))
                    except (OSError, ValueError, sqlite3.Error, CatalogError, OpError) as exc:
                        outcomes.append(ItemOutcome(target, status="failed",
                                                    code=getattr(exc, "code", "IO_ERROR"), message=str(exc)))
                if progress is not None:
                    progress(len(outcomes), len(targets))
        finally:
            with self._lock:
                self._running.pop(job_id, None)
        ok_all = all(o.ok for o in outcomes)
        cancelled_any = any(o.status == "cancelled" for o in outcomes)
        return ServiceResult(
            command_id="files.task.start", ok=ok_all and not cancelled_any,
            accepted=True,
            stage="cancelled" if cancelled_any else "completed",
            job_id=job_id,
            items=tuple(ItemResult(o.source, ok=o.ok, error_code=o.code,
                                   message=o.message, result=o.destination)
                        for o in outcomes),
            revision=self._catalog.revision)

    def _run_one(self, library_id: str, operation: str, target: str,
                 options: dict[str, Any], policy: str,
                 cancel: CancelToken) -> ItemOutcome:
        if operation == "mkdir":
            return self._operations.mkdir(target)
        if operation == "rename":
            return self._operations.rename(
                library_id, target, str(options.get("new_name") or ""),
                policy=policy, cancel=cancel)
        destination = options.get("destination")
        if operation in ("copy", "move"):
            if not destination:
                return ItemOutcome(target, status="failed", code="BAD_OPTIONS",
                                   message="缺少 destination")
            return (self._operations.copy if operation == "copy"
                    else self._operations.move)(
                library_id, target, destination, policy=policy, cancel=cancel)
        if operation == "trash":
            outcome = self._operations.trash(library_id, [target])[0]
            return ItemOutcome(outcome.requested, outcome.restored_to or "",
                               outcome.status, outcome.code, outcome.message)
        if operation == "restore":
            record_id = int(target) if str(target).isdigit() else -1
            if record_id < 0:
                return ItemOutcome(target, status="failed", code="BAD_TARGET",
                                   message="restore 需要回收站记录 id")
            outcome = self._operations.restore(library_id, record_id, policy=policy)
            return ItemOutcome(outcome.requested, outcome.restored_to,
                               outcome.status, outcome.code, outcome.message)
        # purge
        record_id = int(target) if str(target).isdigit() else -1
        if record_id < 0:
            return ItemOutcome(target, status="failed", code="BAD_TARGET",
                                   message="purge 需要回收站记录 id")
        outcome = self._operations.trash_service.purge(record_id, library_id=library_id)
        return ItemOutcome(outcome.requested, "", outcome.status, outcome.code,
                           outcome.message)

    def cancel_task(self, job_id: str) -> ServiceResult:
        with self._lock:
            record = self._running.get(job_id)
        if record is None:
            return rejected("files.task.cancel", error_code="JOB_UNKNOWN",
                            message="任务不在运行中（同步任务通常已完成）")
        record.cancel.request()
        return accepted("files.task.cancel", stage="accepted")

    def cancel_all(self) -> None:
        with self._lock:
            self._closed = True
            for task in self._running.values():
                task.cancel.request()

    def list_trash(self, library_id: str = "") -> list:
        """回收站清单（B4 manifest 一等公民；UI 列表入口用）。"""
        return self._operations.trash_service.list_items(library_id)

    # ------------------------------------------------------------ 中断恢复（B3）
    def pending_recoveries(self) -> list[dict[str, Any]]:
        """上个进程遗留的未终态任务（running/staged/renaming）。"""
        journal = self._operations_journal()
        if journal is None:
            return []
        return journal.pending()

    def recover(self, job_id: str) -> ServiceResult:
        """按磁盘证据分类恢复一个未终态任务；无法判定保持待恢复。"""
        journal = self._operations_journal()
        if journal is None:
            return rejected("files.task.recover", error_code="NO_JOURNAL",
                            message="当前配置没有持久任务日志")
        try:
            verdict = journal.recover_one(str(job_id))
        except Exception as exc:
            return rejected("files.task.recover", error_code="RECOVERY_FAILED",
                            message=str(exc))
        return ServiceResult(
            command_id="files.task.recover", ok=True, accepted=True,
            stage="completed",
            items=(ItemResult(str(job_id), ok=True, result=verdict),),
            revision=self._catalog.revision)

    def _operations_journal(self):
        return getattr(self._operations, "_journal", None)
