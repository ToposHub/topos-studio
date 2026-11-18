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
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.features.files.repository.catalog import CatalogError, FilesCatalog, nfc
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
        with self._catalog._lock:
            return self._trash_locked(source, library_id, retention_days)

    def _trash_locked(self, source: Path, library_id: str,
                      retention_days: int) -> TrashOutcome:
        if source.is_symlink():
            return TrashOutcome(str(source), "failed", "SOURCE_SYMLINK", "拒绝符号链接源")
        if not source.exists():
            return TrashOutcome(str(source), "failed", "SOURCE_MISSING", "源不存在")
        source = Path(os.path.abspath(source))
        is_dir = source.is_dir()
        rows: list[dict[str, Any]] = []
        if is_dir:
            prefix = nfc(source).rstrip("/") + "/"
            with self._catalog._read() as cursor:
                rows = [dict(row) for row in cursor.execute(
                    "SELECT * FROM assets WHERE library_id = ? AND"
                    " (path = ? OR substr(path,1,?) = ?)",
                    (library_id, nfc(source), len(prefix), prefix)).fetchall()]
        else:
            row = self._catalog.asset(library_id, source)
            if row:
                rows = [row]
        if not any(row["path"] == nfc(source) for row in rows):
            rows.insert(0, {"path": nfc(source), "name": source.name,
                            "is_dir": int(is_dir)})
        entries = [{"asset": row, "relations": self._catalog.snapshot_relations(library_id, row["path"])}
                   for row in rows]
        manifest = {"version": MANIFEST_VERSION, "entries": entries,
                    "asset": entries[0]["asset"], "relations": entries[0]["relations"]}
        now = time.time()
        trash_dir = self._locator.trash_dir(library_id)
        trash_dir.mkdir(parents=True, exist_ok=True)
        slot = trash_dir / secrets.token_hex(12)
        slot.mkdir()
        trashed_name = str(Path(slot.name) / source.name)
        try:
            # The complete snapshot is durable before the first destructive move.
            with self._catalog._write() as cursor:
                cursor.execute(
                    "INSERT INTO trash_items (library_id,original_path,item_name,is_dir,"
                    "trashed_name,deleted_at,expire_at,manifest,restore_state)"
                    " VALUES (?,?,?,?,?,?,?,?, 'staging')",
                    (library_id,nfc(source),source.name,int(is_dir),trashed_name,now,
                     now+retention_days*86400,json.dumps(manifest,ensure_ascii=False)))
                record_id = int(cursor.lastrowid)
        except BaseException:
            slot.rmdir()
            raise
        moved = self._ops.move(source, slot, policy="error")
        if not moved.ok:
            with self._catalog._write() as cursor:
                cursor.execute("DELETE FROM trash_items WHERE id = ?", (record_id,))
            slot.rmdir()
            return TrashOutcome(str(source), moved.status, moved.code, moved.message)
        try:
            with self._catalog._write() as cursor:
                for entry in entries:
                    self._catalog.delete_asset_tree(library_id, entry["asset"]["path"])
                cursor.execute("UPDATE trash_items SET restore_state = 'pending' WHERE id = ?", (record_id,))
        except (sqlite3.Error, CatalogError, OSError) as exc:
            return TrashOutcome(str(source), "failed", "TRASH_PENDING_RECOVERY", str(exc), record_id=record_id)
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

    def _payload_path(self, record: TrashRecord) -> Path:
        relative = Path(record.trashed_name)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise CatalogError("MANIFEST_INVALID", "回收站实体路径无效")
        root = self._locator.trash_dir(record.library_id)
        payload = root / relative
        if not payload.resolve().is_relative_to(root.resolve()):
            raise CatalogError("MANIFEST_INVALID", "回收站实体越界")
        if any(parent.is_symlink() for parent in (payload, *payload.parents)):
            raise CatalogError("MANIFEST_INVALID", "回收站路径含符号链接")
        return payload

    def restore(self, record_id: int, *, policy: str = "error") -> TrashOutcome:
        with self._catalog._lock:
            return self._restore_locked(record_id, policy=policy)

    def _restore_locked(self, record_id: int, *, policy: str) -> TrashOutcome:
        record = self.record_for(record_id)
        if record is None:
            return TrashOutcome(str(record_id), "failed", "RECORD_MISSING", "回收站记录不存在")
        if record.manifest.get("version") != MANIFEST_VERSION:
            return TrashOutcome(record.original_path, "failed", "MANIFEST_INVALID", "回收站清单损坏或版本不受支持", record_id=record_id)
        try:
            payload = self._payload_path(record)
        except CatalogError as exc:
            return TrashOutcome(record.original_path,"failed",exc.code,str(exc),record_id=record_id)
        if not payload.exists():
            return TrashOutcome(record.original_path, "failed", "PAYLOAD_MISSING", "回收站实体缺失", record_id=record_id)
        target = Path(record.original_path)
        if not target.is_absolute() or target.name != record.item_name:
            return TrashOutcome(record.original_path,"failed","MANIFEST_INVALID","恢复路径无效",record_id=record_id)
        # Do not overwrite a user's newer object during restore.
        if policy not in ("error", "rename"):
            return TrashOutcome(record.original_path,"failed","BAD_POLICY","恢复仅允许冲突拒绝或生成副本",record_id=record_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        moved = self._ops.move(payload, target.parent, policy=policy)
        if not moved.ok:
            return TrashOutcome(record.original_path,moved.status,moved.code,moved.message,record_id=record_id)
        final_path = Path(moved.destination)
        try:
            with self._catalog._write() as cursor:
                self._restore_identity(record, final_path)
                cursor.execute("DELETE FROM trash_items WHERE id = ?", (record_id,))
        except (sqlite3.Error, CatalogError, OSError) as exc:
            rollback = self._ops.move(final_path, payload.parent, policy="error")
            if rollback.ok and Path(rollback.destination) != payload:
                rollback = self._ops.rename(Path(rollback.destination), payload.name, policy="error")
            if not rollback.ok:
                record.manifest["restore_destination"] = str(final_path)
                with self._catalog._write() as cursor:
                    cursor.execute("UPDATE trash_items SET restore_state = 'restore_pending', manifest = ? WHERE id = ?",
                                   (json.dumps(record.manifest,ensure_ascii=False),record_id))
            return TrashOutcome(record.original_path,"failed","RESTORE_PENDING_RECOVERY",str(exc),record_id=record_id,
                                restored_to=str(final_path) if not rollback.ok else "")
        self._cleanup_slot(payload)
        return TrashOutcome(record.original_path,"done",record_id=record_id,restored_to=str(final_path),relations=record.relations)

    def _restore_identity(self, record: TrashRecord, final_path: Path) -> None:
        entries = record.manifest.get("entries")
        if not isinstance(entries, list) or not entries:
            raise CatalogError("MANIFEST_INVALID", "回收站清单没有完整资产快照")
        counts = {"tags": 0, "collections": 0, "stacks": 0}
        original = Path(record.original_path)
        for entry in entries:
            asset = dict(entry["asset"])
            try:
                relative = Path(asset["path"]).relative_to(original)
            except ValueError as exc:
                raise CatalogError("MANIFEST_INVALID", "资产快照不属于恢复子树") from exc
            restored = final_path / relative
            asset.update(path=nfc(restored), name=restored.name, missing=0)
            self._catalog.insert_asset_full(record.library_id, asset)
            relations = entry.get("relations") or {}
            with self._catalog._write() as cursor:
                for tag in relations.get("tags", []):
                    tag_id = tag["id"]
                    owner = cursor.execute("SELECT library_id FROM tags WHERE id = ?", (tag_id,)).fetchone()
                    if owner is None or owner["library_id"] != record.library_id:
                        raise CatalogError("RELATION_MISSING", "原标签不存在，保留回收站快照供恢复")
                    cursor.execute("INSERT OR IGNORE INTO asset_tags(library_id,path,tag_id) VALUES (?,?,?)",
                                   (record.library_id,nfc(restored),tag_id))
                    counts["tags"] += 1
                for collection in relations.get("collections", []):
                    owner = cursor.execute("SELECT library_id FROM collections WHERE id = ?", (collection["id"],)).fetchone()
                    if owner is None or owner["library_id"] != record.library_id:
                        raise CatalogError("RELATION_MISSING", "原集合不存在，保留回收站快照供恢复")
                    cursor.execute("INSERT OR REPLACE INTO collection_items(collection_id,path,sort_order,added_at) VALUES (?,?,?,?)",
                                   (collection["id"],nfc(restored),collection["sort_order"],collection["added_at"]))
                    counts["collections"] += 1
                for stack in relations.get("stack_details", []):
                    owner = cursor.execute("SELECT library_id FROM stacks WHERE id = ?", (stack["id"],)).fetchone()
                    if owner is None or owner["library_id"] != record.library_id:
                        raise CatalogError("RELATION_MISSING", "原堆叠不存在，保留回收站快照供恢复")
                    cursor.execute("INSERT OR IGNORE INTO stack_members(stack_id,path) VALUES (?,?)",(stack["id"],nfc(restored)))
                    if stack["cover_path"] == entry["asset"]["path"]:
                        cursor.execute("UPDATE stacks SET cover_path = ?, created_at = ? WHERE id = ?",
                                       (nfc(restored),stack["created_at"],stack["id"]))
                    counts["stacks"] += 1
                self._catalog._refresh_fts_tags(cursor,record.library_id,nfc(restored))
                cursor.execute("UPDATE assets_fts SET ocr = ? WHERE library_id = ? AND path = ?",
                               (relations.get("ocr", ""),record.library_id,nfc(restored)))
        record.relations = counts

    def _cleanup_slot(self, payload: Path) -> None:
        if len(payload.relative_to(self._locator.root).parts) == 4:
            try:
                payload.parent.rmdir()
            except OSError:
                pass

    # ------------------------------------------------------------ 彻底删除/过期

    def purge(self, record_id: int, *, library_id: str | None = None) -> TrashOutcome:
        with self._catalog._lock:
            return self._purge_locked(record_id, library_id=library_id)

    def _purge_locked(self, record_id: int, *, library_id: str | None = None) -> TrashOutcome:
        record = self.record_for(record_id)
        if record is None:
            return TrashOutcome(str(record_id), "failed", "RECORD_MISSING",
                                "回收站记录不存在")
        if library_id is not None and library_id != record.library_id:
            return TrashOutcome(str(record_id), "failed", "LIBRARY_MISMATCH",
                                "回收站记录属于另一素材库")
        try:
            payload = self._payload_path(record)
        except CatalogError as exc:
            return TrashOutcome(record.original_path, "failed", exc.code, str(exc), record_id=record_id)
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
        self._cleanup_slot(payload)
        return TrashOutcome(record.original_path, "done", record_id=record_id)

    def purge_expired(self, *, now: float | None = None) -> list[TrashOutcome]:
        now = now if now is not None else time.time()
        results: list[TrashOutcome] = []
        for record in self.list_items():
            if record.expire_at <= now:
                results.append(self.purge(record.id))
        return results
