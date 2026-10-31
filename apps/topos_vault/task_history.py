"""打标任务历史记录（P2-7）。

记录每次 index/cluster/relink 命令的运行，写入 index_history 表。
用途：
    - 事后审计：哪些素材什么时候被打标、花了多久
    - 错误聚合：失败的任务记 detail，方便排查
    - 性能指标：duration_sec 可统计打标吞吐量

用法：
    with TaskHistory.record("index", {"path": "/x", "phased": True}) as h:
        # 跑打标...
        h.update(total=10, done=8, skipped=1, failed=1)
    # 退出 with 块自动写 finished_at + duration + status
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

from apps.topos_vault.paths import get_db_path

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class TaskRun:
    """一次任务运行的记录。"""
    run_id: str
    command: str
    args: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str | None = None
    duration_sec: float | None = None
    status: str = "running"        # running / done / failed
    total: int = 0
    done: int = 0
    skipped: int = 0
    failed: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "command": self.command,
            "args": self.args,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_sec": self.duration_sec,
            "status": self.status,
            "total": self.total,
            "done": self.done,
            "skipped": self.skipped,
            "failed": self.failed,
            "detail": self.detail,
        }


class TaskHistory:
    """任务历史读写。"""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or str(get_db_path())

    @contextmanager
    def record(
        self, command: str, args: dict[str, Any] | None = None,
    ) -> Iterator[TaskRun]:
        """记录一次任务运行的上下文管理器。

        用法：
            with history.record("index", {"path": "/x"}) as run:
                ...  # 跑任务
                run.done = 10
            # 自动写 finished_at + duration + status=done

        异常时 status 自动标 failed + detail 记异常。
        """
        run = TaskRun(
            run_id=str(uuid.uuid4()),
            command=command,
            args=args or {},
            started_at=_now_iso(),
        )
        self._insert(run)
        start_ts = datetime.now(timezone.utc)
        try:
            yield run
            run.status = "done"
        except Exception as e:
            run.status = "failed"
            run.detail["error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            run.finished_at = _now_iso()
            run.duration_sec = (datetime.now(timezone.utc) - start_ts).total_seconds()
            self._finish(run)
            logger.info("任务完成", extra={"extra": run.to_dict()})

    def _insert(self, run: TaskRun) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO index_history
                    (run_id, command, args, started_at, status)
                VALUES (?, ?, ?, ?, 'running')
                """,
                (run.run_id, run.command, json.dumps(run.args, ensure_ascii=False),
                 run.started_at),
            )

    def _finish(self, run: TaskRun) -> None:
        """更新任务的完成信息。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE index_history SET
                    finished_at = ?, duration_sec = ?, status = ?,
                    total = ?, done = ?, skipped = ?, failed = ?, detail = ?
                WHERE run_id = ?
                """,
                (run.finished_at, run.duration_sec, run.status,
                 run.total, run.done, run.skipped, run.failed,
                 json.dumps(run.detail, ensure_ascii=False), run.run_id),
            )

    def list_runs(self, limit: int = 50) -> list[TaskRun]:
        """列出最近的任务运行（按开始时间倒序，rowid 兜底保稳定序）。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM index_history ORDER BY started_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_run(r) for r in rows]

    def get_run(self, run_id: str) -> TaskRun | None:
        """按 run_id 查单次运行。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM index_history WHERE run_id = ?", (run_id,),
            ).fetchone()
        return self._row_to_run(row) if row else None

    def aggregate_stats(self) -> dict[str, Any]:
        """聚合统计（总运行数/成功率/平均耗时）。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT
                    COUNT(*) as total_runs,
                    SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) as success,
                    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
                    AVG(duration_sec) as avg_duration,
                    SUM(total) as total_items,
                    SUM(done) as done_items
                FROM index_history
                WHERE status != 'running'
                """
            ).fetchone()
        if not row or row["total_runs"] == 0:
            return {"total_runs": 0}
        return {
            "total_runs": int(row["total_runs"]),
            "success": int(row["success"] or 0),
            "failed": int(row["failed"] or 0),
            "success_rate": (int(row["success"] or 0) / int(row["total_runs"])),
            "avg_duration_sec": float(row["avg_duration"] or 0),
            "total_items": int(row["total_items"] or 0),
            "done_items": int(row["done_items"] or 0),
        }

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> TaskRun:
        args = row["args"]
        detail = row["detail"]
        return TaskRun(
            run_id=row["run_id"],
            command=row["command"],
            args=json.loads(args) if args else {},
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            duration_sec=row["duration_sec"],
            status=row["status"],
            total=row["total"],
            done=row["done"],
            skipped=row["skipped"],
            failed=row["failed"],
            detail=json.loads(detail) if detail else {},
        )
