"""统一文件操作领域入口（B 阶段，总计划 B 表第 3 行）。

单改名、任务 move/rename、回收站恢复全部经 ``FileOperationService``：
B2 安全事务执行 + B3 journal 记录 + B5 身份/关系随行在同一次调用内完成。
UI/任务层不得再自建文件系统写路径（C 阶段 facade 只代理本服务）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.features.files.repository.catalog import FilesCatalog, nfc
from src.features.files.transactions.journal import OperationJournal
from src.features.files.transactions.ops import (
    CancelToken,
    FileTransactionService,
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
        """文件走 move_asset_rows，目录走 move_subtree；幂等（无行则 0）。

        回调在移动完成后执行：目录判断看新路径（旧路径已不存在）。
        """
        old_path, new_path = Path(old), Path(new)
        if new_path.is_dir() or (not new_path.exists() and old_path.is_dir()):
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
                  executor, *, intended_destination: Path | None = None) -> ItemOutcome:
        handle = None
        if self._journal is not None:
            args = {"source": str(source)}
            if intended_destination is not None:
                # B3 恢复证据：意图目的地（策略改名会有后缀，证据分类按
                # 存在性 best-effort 判定）
                args["destination"] = str(intended_destination)
            handle = self._journal.begin(
                library_id, op, args,
                idempotency_key=f"{op}:{nfc(source)}",
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
                relations = self._propagation.on_moved(
                    library_id, result.source, result.destination)
            outcome = ItemOutcome(result.source, result.destination, result.status,
                                  result.code, result.message, relations)
            return outcome
        finally:
            if self._journal is not None and outcome is not None:
                state = ("completed" if outcome.ok else
                         "cancelled" if outcome.status == "cancelled" else "failed")
                self._journal.finish(state, outcome.code)

    # ------------------------------------------------------------ 回收站

    def mkdir(self, target: str | Path) -> ItemOutcome:
        """新建目录（无身份迁移）；冲突/类型错误走统一词表。"""
        result = self._ops.mkdir(target)
        return ItemOutcome(result.source, result.destination, result.status,
                           result.code, result.message)

    def trash(self, library_id: str, paths: list[str | Path]) -> list[TrashOutcome]:
        return self.trash_service.trash(paths, library_id)

    def restore(self, library_id: str, record_id: int, *,
                policy: str = "error") -> TrashOutcome:
        return self.trash_service.restore(record_id, policy=policy)
