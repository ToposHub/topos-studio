"""M15.1b · 自动备份调度器（工程级命名快照的定时拍备）。

规则（对齐计划 §M15.1：自动备份/命名版本/调色版本三层区分）：
- **auto_backup=True** 的快照可覆盖同名（take_snapshot 既有语义）；
- 调度器只在**工程有实质变更**（revision 前进）时拍备——同 revision
  空转不产生重复备份；
- 备份名固定 ``__auto_backup__``（单一滚动备份点，不堆历史——历史
  走用户命名版本）；
- 拍备失败（如 payload 含 secret 被拒）留日志不崩，下次再试。

N09 修复（审计 2026-10-25 §2 N09）：工程不可变——``take_snapshot``
返回**带快照的新工程**；旧实现丢弃新工程仅计数返回 True，快照从未
进入在册工程（虚报成功）。现契约：

- ``tick`` 返回**新工程对象**（调用方必须写回在册工程，快照才算
  落库），未到期/无变更/失败返回 None；
- ``backups_taken`` 只在调用方写回后由 ``confirm_written`` 计数——
  "拍了"与"落库了"分离，未确认的拍备不虚计；
- ``last_backup_meta`` 供诊断（快照名/来源 revision/payload hash）。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from src.features.edit.core.project_snapshots import take_snapshot

logger = logging.getLogger(__name__)

AUTO_BACKUP_NAME = "__auto_backup__"


class SnapshotBackupScheduler:
    """轮询式自动备份（由宿主 QTimer/工作线程周期调 :meth:`tick`）。"""

    def __init__(
        self,
        *,
        interval_seconds: float = 300.0,
        max_snapshots: int = 1,
    ) -> None:
        self._interval = max(10.0, float(interval_seconds))
        self._last_backup_revision: Optional[int] = None
        self._last_backup_at = 0.0
        self._backups_taken = 0
        self._backups_written = 0
        self._last_error = ""
        self._last_backup_meta: Optional[dict] = None
        self._pending_new_project: Optional[Any] = None

    @property
    def interval_seconds(self) -> float:
        return self._interval

    @property
    def backups_taken(self) -> int:
        return self._backups_taken

    @property
    def backups_written(self) -> int:
        """已确认写回在册工程的备份数（真实成功口径，N09）。"""
        return self._backups_written

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def last_backup_meta(self) -> Optional[dict]:
        return self._last_backup_meta

    def tick(self, project: Any, *, now: Optional[float] = None,
             force: bool = False) -> Optional[Any]:
        """一次调度心跳：到期且 revision 前进则拍 auto_backup。

        Returns:
            **带快照的新工程**（调用方必须写回在册工程后调
            :meth:`confirm_written`）；None=未到期/无变更/失败（失败
            原因在 ``last_error``）。
        """
        current_time = float(now if now is not None else time.time())
        if not force and (current_time - self._last_backup_at) < self._interval:
            return None
        revision = int(getattr(project, "revision", 0) or 0)
        if (not force) and revision == self._last_backup_revision:
            return None  # 同 revision 空转——不堆重复备份
        try:
            new_project, meta = take_snapshot(
                project, AUTO_BACKUP_NAME, auto_backup=True)
        except Exception as exc:  # noqa: BLE001 — 失败不崩，留待下次
            self._last_error = str(exc)
            self._last_backup_at = current_time  # 失败也刷新节拍防风暴
            logger.warning("自动备份失败（下次再试）：%s", exc)
            return None
        self._last_backup_revision = revision
        self._last_backup_at = current_time
        self._last_error = ""
        self._pending_new_project = new_project
        self._last_backup_meta = {
            "name": AUTO_BACKUP_NAME,
            "source_revision": getattr(meta, "source_revision", revision),
            "payload_hash": getattr(meta, "payload_hash", ""),
        }
        return new_project

    def confirm_written(self) -> None:
        """调用方把 tick 返回的新工程写回在册工程后确认（计数落库）。"""
        if self._pending_new_project is not None:
            self._backups_taken += 1
            self._backups_written += 1
            self._pending_new_project = None


__all__ = ["AUTO_BACKUP_NAME", "SnapshotBackupScheduler"]
