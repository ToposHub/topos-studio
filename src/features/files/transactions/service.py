"""统一文件操作领域入口（B 阶段，总计划 B 表第 3 行）。

单改名、任务 move/rename、回收站恢复全部经 ``FileOperationService``：
B2 安全事务执行 + B3 journal 记录 + B5 身份/关系随行在同一次调用内完成。
UI/任务层不得再自建文件系统写路径（C 阶段 facade 只代理本服务）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
import sqlite3

from src.features.files.contracts import is_valid_folder_name
from src.features.files.repository.catalog import CatalogError, FilesCatalog, LibraryRecord, nfc
from src.features.files.transactions.journal import OperationJournal
from src.features.files.transactions.ops import (
    CancelToken,
    FileTransactionService,
    OpError,
)
from src.features.files.transactions.trash_service import TrashOutcome, TrashService


@dataclass
class ItemOutcome:
    """单条目结果：与传输层 stages 对齐的稳定词表。"""

    source: str
    destination: str = ""
    status: str = "done"  # done | conflict | failed | cancelled
    code: str = ""
    message: str = ""
    relations_moved: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "done"


class IdentityPropagation:
    """B5：改名/移动后的库内身份与关系随行。"""

    def __init__(self, catalog: FilesCatalog) -> None:
        self._catalog = catalog

    def on_moved(self, library_id: str, old: str, new: str) -> int:
        """Update the catalog ownership and path after a published move.

        回调在移动完成后执行：目录判断看新路径（旧路径已不存在）。
        """
        if not library_id:
            return 0
        old_path, new_path = Path(old), Path(new)
        is_dir = new_path.is_dir() or (not new_path.exists() and old_path.is_dir())
        target = self._catalog.library_for_path(new_path)
        if target is not None and target.library_id != library_id:
            return self._catalog.move_to_library(library_id, target.library_id, old_path, new_path, is_dir=is_dir)
        if is_dir:
            counts = self._catalog.move_subtree(library_id, old_path, new_path)
            return counts["assets"]
        moved = self._catalog.move_asset_rows(library_id, old_path, new_path)
        if moved:
            self._catalog.restore_relations(library_id, new_path, old_path=old_path)
        return moved


class FileOperationService:
    """唯一生产写入口（改名/移动/复制/回收站）。"""

    def __init__(self, catalog: FilesCatalog, *,
                 journal: OperationJournal | None = None) -> None:
        self._catalog = catalog
        self._journal = journal
        self._ops = FileTransactionService(journal=journal)
        self._propagation = IdentityPropagation(catalog)
        self.trash_service = TrashService(catalog, catalog.locator, ops=self._ops)

    # ------------------------------------------------------------ 移动/改名/复制

    def move(self, library_id: str, source: str | Path, destination_dir: str | Path,
             *, policy: str = "error", cancel: CancelToken | None = None) -> ItemOutcome:
        return self._transfer("move", library_id, source,
                              lambda: self._ops.move(
                                  source, destination_dir, policy=policy, cancel=cancel,
                                  on_identity_moved=None),
                              intended_destination=Path(destination_dir) / Path(source).name)

    def rename(self, library_id: str, source: str | Path, new_name: str,
               *, policy: str = "error", cancel: CancelToken | None = None) -> ItemOutcome:
        return self._transfer("rename", library_id, source,
                              lambda: self._ops.rename(
                                  source, new_name, policy=policy, cancel=cancel,
                                  on_identity_moved=None),
                              intended_destination=Path(source).parent / new_name)

    def copy(self, library_id: str, source: str | Path, destination_dir: str | Path,
             *, policy: str = "error", cancel: CancelToken | None = None) -> ItemOutcome:
        return self._transfer("copy", library_id, source,
                              lambda: self._ops.copy(
                                  source, destination_dir, policy=policy, cancel=cancel),
                              intended_destination=Path(destination_dir) / Path(source).name)

    def _transfer(self, op: str, library_id: str, source: str | Path,
                  executor: Callable[[], Any], *, intended_destination: Path | None = None) -> ItemOutcome:
        # Shared with maintenance: no backup can observe a published filesystem
        # rename without its catalog propagation (or explicit recovery record).
        with self._catalog._lock:
            return self._transfer_locked(op, library_id, source, executor,
                                         intended_destination=intended_destination)

    def _transfer_locked(self, op: str, library_id: str, source: str | Path,
                         executor: Callable[[], Any], *,
                         intended_destination: Path | None) -> ItemOutcome:
        handle = None
        if self._journal is not None:
            args = {"source": str(source)}
            if intended_destination is not None:
                # B3 恢复证据：意图目的地（策略改名会有后缀，证据分类按
                # 存在性 best-effort 判定）
                args["destination"] = str(intended_destination)
            handle = self._journal.begin(
                library_id, op, args,
                # A pathname is not a request identity. A later file at the same
                # name, or a different destination, must not replay this result.
                idempotency_key="",
            )
            if handle.replayed:
                existing = self._journal.get(handle.job_id) or {}
                if existing.get("state") == "completed":
                    return ItemOutcome(str(source), status="done",
                                       code="IDEMPOTENT_REPLAY",
                                       message="同键任务已完成，未重复执行")
                return ItemOutcome(str(source), status="failed",
                                   code="IDEMPOTENT_INFLIGHT",
                                   message="同键任务尚未终态，拒绝并发重复提交")
        outcome: ItemOutcome | None = None
        try:
            result = executor()
            relations = 0
            if result.ok and op != "copy":
                if self._journal is not None:
                    self._journal.record(op, "fs_published", {
                        "source": result.source, "destination": result.destination})
                try:
                    with self._catalog._write():
                        relations = self._propagation.on_moved(
                            library_id, result.source, result.destination)
                except (sqlite3.Error, CatalogError, OSError) as exc:
                    outcome = ItemOutcome(result.source, result.destination, "failed",
                                          "METADATA_PENDING_RECOVERY", str(exc))
                    return outcome
            outcome = ItemOutcome(result.source, result.destination, result.status,
                                  result.code, result.message, relations)
            return outcome
        finally:
            if self._journal is not None and outcome is not None:
                state = ("pending_recovery" if outcome.code == "METADATA_PENDING_RECOVERY" else
                         "completed" if outcome.ok else
                         "cancelled" if outcome.status == "cancelled" else "failed")
                self._journal.finish(state, outcome.code)

    # ------------------------------------------------------------ 回收站

    def mkdir(self, target: str | Path, *, parents: bool = True) -> ItemOutcome:
        """新建目录（无身份迁移）；冲突/类型错误走统一词表。"""
        with self._catalog._lock:
            result = self._ops.mkdir(target, parents=parents)
        return ItemOutcome(result.source, result.destination, result.status,
                           result.code, result.message)

    def create_library(self, parent: str, name: str) -> LibraryRecord:
        name = name.strip()
        if not is_valid_folder_name(name):
            raise ValueError("请输入有效的单级文件夹名称。")
        target = Path(parent).resolve(strict=True) / name
        with self._catalog._lock:
            result = self._ops.mkdir(target, parents=False)
            if not result.ok:
                raise OpError(result.code, result.message)
            created = target.lstat()
            try:
                return self._catalog.register_library(target, name)
            except (OSError, ValueError, CatalogError) as exc:
                try:
                    current = target.lstat()
                    if (created.st_dev, created.st_ino) == (current.st_dev, current.st_ino):
                        target.rmdir()
                except FileNotFoundError:
                    pass
                except OSError as cleanup_error:
                    raise RuntimeError(f"素材库登记失败，已创建的文件夹仍在 {target}。{exc}") from cleanup_error
                raise

    def trash(self, library_id: str, paths: list[str | Path]) -> list[TrashOutcome]:
        return self.trash_service.trash(paths, library_id)

    def restore(self, library_id: str, record_id: int, *,
                policy: str = "error") -> TrashOutcome:
        record = self.trash_service.record_for(record_id)
        if record is not None and record.library_id != library_id:
            return TrashOutcome(str(record_id), "failed", "LIBRARY_MISMATCH", "回收站记录属于另一素材库")
        return self.trash_service.restore(record_id, policy=policy)
