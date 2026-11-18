"""持久操作日志（B 阶段，总计划 B 表第 4/10 行）。

文件系统与 catalog.db 无法组成单一原子事务：本模块以 job_log 表记录
每次操作的 staging/发布/完成事件，提供幂等键重放、中断后的**证据分类
恢复**与有界历史。诚实规则：

- 只有操作真实成功才 ``finish("completed")``；日志写失败立刻抛出，
  绝不"日志报错后仍宣称完整成功"。
- 恢复只依据磁盘证据分类（目标在/源在/残留 tmp），无法判定的一律
  保持 ``pending_recovery``，不猜测执行。
"""
from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.features.files.repository.catalog import FilesCatalog

TERMINAL_STATES = ("completed", "failed", "cancelled", "pending_recovery")
ACTIVE_STATES = ("running", "staged", "renaming")


class JournalError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class JobHandle:
    job_id: str
    replayed: bool = False  # True = 幂等键命中既有任务，未新开


class OperationJournal:
    """job_log 的独占写者；FileTransactionService 经 IdentityJournal 协议写入事件。"""

    def __init__(self, catalog: FilesCatalog, *, history_limit: int = 200) -> None:
        self._catalog = catalog
        self._history_limit = history_limit
        self._active: dict[int, str] = {}  # thread ident -> job_id（进程内关联）

    # ------------------------------------------------------------ 生命周期

    def begin(self, library_id: str, op: str, args: dict[str, Any],
              *, idempotency_key: str = "") -> JobHandle:
        with self._catalog._lock:
            return self._begin_locked(library_id, op, args, idempotency_key=idempotency_key)

    def _begin_locked(self, library_id: str, op: str, args: dict[str, Any],
                      *, idempotency_key: str) -> JobHandle:
        """开任务；同 (library_id, idempotency_key) 已存在则重放既有任务。"""
        if idempotency_key:
            existing = self._find_by_key(library_id, idempotency_key)
            if existing is not None:
                handle = JobHandle(existing["id"], replayed=True)
                return handle
        job_id = f"job-{secrets.token_hex(8)}"
        now = time.time()
        with self._catalog._write() as cursor:
            cursor.execute(
                "INSERT INTO job_log (id, library_id, op, args, idempotency_key,"
                " state, items, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'running', '[]', ?, ?)",
                (job_id, library_id, op, json.dumps(args, ensure_ascii=False),
                 idempotency_key or None, now, now),
            )
        self._active[self._thread_key()] = job_id
        return JobHandle(job_id)

    def finish(self, state: str, note: str = "") -> None:
        """终态收口；state 必须属于 TERMINAL_STATES，且由操作结果决定。"""
        if state not in TERMINAL_STATES:
            raise JournalError("BAD_STATE", f"非法终态 {state}")
        job_id = self._current_job()
        with self._catalog._write() as cursor:
            row = cursor.execute(
                "SELECT items FROM job_log WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise JournalError("JOB_MISSING", f"任务 {job_id} 不存在")
            items = json.loads(row["items"] or "[]")
            if note:
                items.append({"state": state, "note": note, "at": time.time()})
            cursor.execute(
                "UPDATE job_log SET state = ?, items = ?, updated_at = ? WHERE id = ?",
                (state, json.dumps(items, ensure_ascii=False), time.time(), job_id),
            )
        self._active.pop(self._thread_key(), None)
        self.trim()

    # ------------------------------------------------------------ IdentityJournal 协议

    def record(self, op: str, state: str, detail: dict[str, Any]) -> None:
        """ops.py 的事件入口：staged/renaming/done 追加进当前任务。"""
        thread_key = self._thread_key()
        job_id = self._active.get(thread_key)
        if job_id is None:
            return  # 无活动任务（纯内存用法）：静默不记，不阻塞 FS 操作
        if state in ("done", "fs_published") and detail.get("destination"):
            destination = Path(detail["destination"])
            identity = destination.lstat()
            detail = {**detail, "identity": [identity.st_dev, identity.st_ino,
                                              identity.st_size, identity.st_mtime_ns]}
        items_update = json.dumps(
            self._append_event(job_id, op, state, detail), ensure_ascii=False
        )
        with self._catalog._write() as cursor:
            if state in ACTIVE_STATES:
                cursor.execute(
                    "UPDATE job_log SET items = ?, state = ?, updated_at = ? WHERE id = ?",
                    (items_update, state, time.time(), job_id),
                )
            else:
                cursor.execute(
                    "UPDATE job_log SET items = ?, updated_at = ? WHERE id = ?",
                    (items_update, time.time(), job_id),
                )

    def _append_event(self, job_id: str, op: str, state: str,
                      detail: dict[str, Any]) -> list:
        with self._catalog._read() as cursor:
            row = cursor.execute(
                "SELECT items FROM job_log WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise JournalError("JOB_MISSING", f"任务 {job_id} 不存在")
        items = json.loads(row["items"] or "[]")
        items.append({"op": op, "state": state, **detail, "at": time.time()})
        return items

    # ------------------------------------------------------------ 查询

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._catalog._read() as cursor:
            row = cursor.execute(
                "SELECT * FROM job_log WHERE id = ?", (job_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def pending(self) -> list[dict[str, Any]]:
        with self._catalog._read() as cursor:
            rows = cursor.execute(
                "SELECT * FROM job_log WHERE state IN ('running','staged','renaming','pending_recovery')"
                " ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def _find_by_key(self, library_id: str, key: str) -> dict[str, Any] | None:
        with self._catalog._read() as cursor:
            row = cursor.execute(
                "SELECT * FROM job_log WHERE library_id = ? AND idempotency_key = ?",
                (library_id, key),
            ).fetchone()
        return dict(row) if row is not None else None

    # ------------------------------------------------------------ 恢复（证据分类）

    def recover_one(self, job_id: str) -> dict[str, str]:
        """按磁盘证据分类一个未终态任务；无法判定保持 pending_recovery。

        - completed：move 的目标已存在且源不在（或 copy 目标已存在）。
        - failed.retriable：残留 staging tmp 存在 → 清理 tmp，任务可重试。
        - pending_recovery：证据不足（源/目标都在或都不在且无 tmp）。
        """
        job = self.get(job_id)
        if job is None:
            raise JournalError("JOB_MISSING", f"任务 {job_id} 不存在")
        if job["state"] in ("completed", "failed", "cancelled"):
            return {"job_id": job_id, "classification": job["state"], "action": "none"}
        items = json.loads(job["items"] or "[]")
        args = json.loads(job["args"] or "{}")
        published = next((item for item in reversed(items)
                          if item.get("state") in ("done", "fs_published") and item.get("identity")), None)
        source_text = published.get("source") if published else args.get("source")
        destination_text = published.get("destination") if published else args.get("destination")
        source = Path(source_text) if source_text else None
        destination = Path(destination_text) if destination_text else None
        last_staged = next(
            (item for item in reversed(items) if item.get("state") == "staged"), None
        )
        tmp = Path(last_staged["tmp"]) if last_staged and last_staged.get("tmp") else None

        src_exists = source is not None and (source.exists() or source.is_symlink())
        dst_exists = destination is not None and (destination.exists() or destination.is_symlink())
        action = "none"
        verified_publication = False
        if dst_exists and published:
            current = destination.lstat()
            verified_publication = [current.st_dev,current.st_ino,current.st_size,current.st_mtime_ns] == published["identity"]
        if verified_publication and (not src_exists or job["op"] == "copy"):
            if job["op"] in ("move", "rename"):
                from src.features.files.transactions.service import IdentityPropagation
                with self._catalog._write():
                    IdentityPropagation(self._catalog).on_moved(job["library_id"],str(source),str(destination))
            state, classification = "completed", "completed"
            action = "reconciled_catalog"
        elif dst_exists and src_exists:
            state, classification = "pending_recovery", "ambiguous"
        else:
            state, classification = "pending_recovery", "no_evidence"
        self._set_state(job_id, state,
                        f"recovery:{classification}" + (f":{action}" if action != "none" else ""))
        return {"job_id": job_id, "classification": classification, "action": action,
                "state": state}

    def _set_state(self, job_id: str, state: str, note: str) -> None:
        with self._catalog._write() as cursor:
            row = cursor.execute("SELECT items FROM job_log WHERE id = ?", (job_id,)).fetchone()
            items = json.loads(row["items"] or "[]") if row is not None else []
            items.append({"state": state, "note": note, "at": time.time()})
            cursor.execute(
                "UPDATE job_log SET state = ?, items = ?, updated_at = ? WHERE id = ?",
                (state, json.dumps(items, ensure_ascii=False), time.time(), job_id),
            )

    # ------------------------------------------------------------ 有界历史

    def trim(self) -> int:
        """终态任务只保留最近 history_limit 条；未终态永不淘汰。"""
        with self._catalog._write() as cursor:
            kept = cursor.execute(
                "SELECT COUNT(*) FROM job_log WHERE state IN"
                " ('completed','failed','cancelled')"
            ).fetchone()[0]
            if kept <= self._history_limit:
                return 0
            cursor.execute(
                "DELETE FROM job_log WHERE id IN ("
                "  SELECT id FROM job_log WHERE state IN"
                "  ('completed','failed','cancelled')"
                "  ORDER BY updated_at DESC LIMIT -1 OFFSET ?)",
                (self._history_limit,),
            )
            return cursor.rowcount

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _thread_key() -> int:
        import threading

        return threading.get_ident()

    def _current_job(self) -> str:
        job_id = self._active.get(self._thread_key())
        if job_id is None:
            raise JournalError("NO_ACTIVE_JOB", "当前线程没有活动任务")
        return job_id
