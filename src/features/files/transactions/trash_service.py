"""回收站：版本化完整清单与恢复冲突语义（B 阶段，总计划 B 表第 6 行）。

规则：
- **快照失败禁止删除**：清单（资产全列 + 标签 + 集合成员 + 堆叠成员）
  落库成功才移动文件；移动失败则库内行原样保留、可重试。
- 恢复按清单重建全部关系；目标被占时 error 策略保持回收站完整可重试，
  rename 策略落到唯一新名并把关系挂到**最终身份**。
- 每库独立 ``files-data/trash/<library_id>/``；payload 经 B2 安全移动。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.features.files.repository.catalog import FilesCatalog, nfc
from src.features.files.repository.locator import FilesDataLocator
from src.features.files.transactions.ops import FileTransactionService

MANIFEST_VERSION = 1
DEFAULT_RETENTION_DAYS = 30


@dataclass
class TrashRecord:
    id: int
    library_id: str
    original_path: str
    item_name: str
    is_dir: bool
    trashed_name: str
    deleted_at: float
    expire_at: float
    manifest: dict[str, Any] = field(default_factory=dict)


@dataclass
class TrashOutcome:
    requested: str
    status: str  # done | conflict | failed
    code: str = ""
    message: str = ""
    record_id: int | None = None
    restored_to: str = ""
    relations: dict[str, int] | None = None

    @property
    def ok(self) -> bool:
        return self.status == "done"


class TrashService:
    def __init__(self, catalog: FilesCatalog, locator: FilesDataLocator,
                 *, ops: FileTransactionService | None = None) -> None:
        self._catalog = catalog
        self._locator = locator
        self._ops = ops or FileTransactionService()

    # ------------------------------------------------------------ 入站

    def trash(self, paths: list[str | Path], library_id: str, *,
              retention_days: int = DEFAULT_RETENTION_DAYS) -> list[TrashOutcome]:
        results: list[TrashOutcome] = []
        for raw in paths:
            results.append(self._trash_one(Path(raw), library_id, retention_days))
        return results

    def _trash_one(self, source: Path, library_id: str,
                   retention_days: int) -> TrashOutcome:
        if not source.exists() and not source.is_symlink():
            return TrashOutcome(str(source), "failed", "SOURCE_MISSING", "源不存在")
        is_dir = source.is_dir()
        asset_row = self._catalog.asset(library_id, source)
        relations = self._catalog.snapshot_relations(library_id, source)
        manifest = {
            "version": MANIFEST_VERSION,
            "asset": asset_row or {"path": nfc(source), "name": source.name},
            "relations": relations,
        }
        now = time.time()
        trash_dir = self._locator.trash_dir(library_id)
        trash_dir.mkdir(parents=True, exist_ok=True)
        moved = self._ops.move(source, trash_dir, policy="rename")
        if not moved.ok:
            return TrashOutcome(str(source), moved.status, moved.code, moved.message)
        trashed_name = Path(moved.destination).name
        with self._catalog._write() as cursor:
            cursor.execute(
                "INSERT INTO trash_items (library_id, original_path, item_name,"
                " is_dir, trashed_name, deleted_at, expire_at, manifest)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (library_id, nfc(source), source.name,
                 1 if is_dir else 0, trashed_name, now,
                 now + retention_days * 86400,
                 json.dumps(manifest, ensure_ascii=False)),
            )
            record_id = int(cursor.lastrowid)
        # 文件已入站后才移除活动行；此刻起恢复信息只在清单里
        self._catalog.delete_asset_tree(library_id, source)
        return TrashOutcome(str(source), "done", record_id=record_id)

    # ------------------------------------------------------------ 查询

    def list_items(self, library_id: str = "") -> list[TrashRecord]:
        with self._catalog._read() as cursor:
            if library_id:
                rows = cursor.execute(
                    "SELECT * FROM trash_items WHERE library_id = ?"
                    " ORDER BY deleted_at DESC", (library_id,)).fetchall()
            else:
                rows = cursor.execute(
                    "SELECT * FROM trash_items ORDER BY deleted_at DESC").fetchall()
        records = []
        for row in rows:
            data = dict(row)
            try:
                manifest = json.loads(data.get("manifest") or "{}")
            except ValueError:
                manifest = {}
            records.append(TrashRecord(
                id=data["id"], library_id=data["library_id"],
                original_path=data["original_path"], item_name=data["item_name"],
                is_dir=bool(data["is_dir"]), trashed_name=data["trashed_name"],
                deleted_at=data["deleted_at"], expire_at=data["expire_at"],
                manifest=manifest,
            ))
        return records

    def record_for(self, record_id: int) -> TrashRecord | None:
        for record in self.list_items():
            if record.id == record_id:
                return record
        return None

    # ------------------------------------------------------------ 恢复

    def restore(self, record_id: int, *, policy: str = "error") -> TrashOutcome:
        record = self.record_for(record_id)
        if record is None:
            return TrashOutcome(str(record_id), "failed", "RECORD_MISSING",
                                "回收站记录不存在")
        payload = self._locator.trash_dir(record.library_id) / record.trashed_name
        if not payload.exists():
            return TrashOutcome(record.original_path, "failed", "PAYLOAD_MISSING",
                                "回收站实体缺失", record_id=record_id)
        target = Path(record.original_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        moved = self._ops.move(payload, target.parent, policy=policy)
        if not moved.ok:
            # 冲突/失败：回收站记录与实体保持完整，可重试
            return TrashOutcome(record.original_path, moved.status, moved.code,
                                moved.message, record_id=record_id)
        final_path = Path(moved.destination)
        self._restore_identity(record, final_path)
        with self._catalog._write() as cursor:
            cursor.execute("DELETE FROM trash_items WHERE id = ?", (record_id,))
        return TrashOutcome(record.original_path, "done", record_id=record_id,
                            restored_to=str(final_path))

    def _restore_identity(self, record: TrashRecord, final_path: Path) -> None:
        manifest_asset = dict(record.manifest.get("asset") or {})
        manifest_asset["path"] = nfc(final_path)
        manifest_asset["name"] = final_path.name
        manifest_asset["missing"] = 0
        self._catalog.insert_asset_full(record.library_id, manifest_asset)
        relations = record.manifest.get("relations") or {}
        counts = self._catalog.restore_relations(
            record.library_id, final_path,
            tag_names=[tag["name"] for tag in relations.get("tags", [])],
            collection_ids=[item["id"] for item in relations.get("collections", [])],
            stack_ids=list(relations.get("stacks", [])),
        )
        record.relations = counts

    # ------------------------------------------------------------ 彻底删除/过期

    def purge(self, record_id: int) -> TrashOutcome:
        record = self.record_for(record_id)
        if record is None:
            return TrashOutcome(str(record_id), "failed", "RECORD_MISSING",
                                "回收站记录不存在")
        payload = self._locator.trash_dir(record.library_id) / record.trashed_name
        try:
            if payload.is_dir() and not payload.is_symlink():
                import shutil

                shutil.rmtree(payload)
            elif payload.exists() or payload.is_symlink():
                payload.unlink()
        except OSError as exc:
            return TrashOutcome(record.original_path, "failed", "IO_ERROR",
                                str(exc), record_id=record_id)
        with self._catalog._write() as cursor:
            cursor.execute("DELETE FROM trash_items WHERE id = ?", (record_id,))
        return TrashOutcome(record.original_path, "done", record_id=record_id)

    def purge_expired(self, *, now: float | None = None) -> list[TrashOutcome]:
        now = now if now is not None else time.time()
        results: list[TrashOutcome] = []
        for record in self.list_items():
            if record.expire_at <= now:
                results.append(self.purge(record.id))
        return results
