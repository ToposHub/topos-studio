"""R01／BR01：静帧抓取的多文件保存事务（staging → 备份 → 安装 → 提交）。

契约（:class:`StillCaptureTransaction`）：

- 一次抓帧 = 一个事务：新文件全部先写入目标目录内的隐藏 staging 目录
  （同卷，单文件 ``os.replace`` 原子），现有目标在提交前一律不动；
- **意图先行**（BR01）：备份路径在 ``os.replace`` 之前登记进 manifest；
  条目同时登记旧内容身份（size＋mtime_ns）。任何崩溃窗口内，恢复都
  能从 journal 意图 × 磁盘实际状态 × 内容身份判定唯一副本在哪；
- 提交按 manifest 日志分阶段推进（prepared → backing → installing →
  committed），每次状态/条目变更先落盘再动文件；中途异常立即回滚，
  进程退出的遗留事务由 :func:`recover_still_transactions` 在下一次事务
  开始时收敛到"完整旧集合或完整新集合"；
- **保全优先**（BR01）：回滚/恢复的单项失败保留事务目录与备份（供重试
  或人工恢复），只有全部后置条件验证成功才清理；坏 manifest／未知
  schema／目标路径越界一律隔离不动（outcome=quarantined）；
- 同一目标目录的并发事务被目录互斥挡住（忙＝明确的
  :class:`StillCaptureTransactionError`，不是互相破坏 staging）；
- 只清理本事务拥有的 staging 目录，禁止按固定目标名删除既有资产；
- 本次集合不包含的旧 sidecar（如降级未写 EXR）按 ``drop`` 条目处理：
  提交后不保留过期副本，回滚时照常恢复。
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import uuid
from typing import Any, Dict, List, Optional, Tuple

from src.features.color.core.gallery_txn_core import (
    OUTCOME_CONFLICT,
    OUTCOME_PENDING,
    OUTCOME_QUARANTINED,
    OUTCOME_RECOVERED,
    TXN_JOURNAL_SCHEMA,
    DirectoryBusyError,
    JournalError,
    TxnRecoveryResult,
    acquire_directories,
    file_identity,
    fsync_dir,
    fsync_file,
    identity_matches,
    make_owner_block,
    other_process_alive,
    path_within,
    read_journal,
    release_directories,
    try_recover_lock,
    write_journal,
)

logger = logging.getLogger(__name__)

TXN_DIR_PREFIX = ".still-txn-"
MANIFEST_NAME = "manifest.json"
_STAGED_SUBDIR = "staged"
_BACKUP_SUBDIR = "backup"

STATE_PREPARED = "prepared"
STATE_BACKING = "backing"
STATE_INSTALLING = "installing"
STATE_COMMITTED = "committed"
STATE_ROLLED_BACK = "rolled_back"

_UNCOMMITTED_STATES = (STATE_PREPARED, STATE_BACKING, STATE_INSTALLING)


class StillCaptureTransactionError(RuntimeError):
    """事务推进失败（回滚已执行或 staging 状态不满足前置条件）。"""


class _EntryRestoreError(RuntimeError):
    """单条目无法安全收敛（消息稳定描述现场，事务必须保全）。"""

    def __init__(self, outcome: str, message: str) -> None:
        super().__init__(message)
        self.outcome = outcome


def _manifest_path(txn_dir: str) -> str:
    return os.path.join(txn_dir, MANIFEST_NAME)


def _norm_identity(raw: Any) -> Optional[Tuple[int, int]]:
    if isinstance(raw, (list, tuple)) and len(raw) == 2 \
            and all(isinstance(v, int) for v in raw):
        return int(raw[0]), int(raw[1])
    return None


def _restore_entry_to_old_set(entry: Dict[str, Any]) -> None:
    """把单个条目收敛回旧集合——按实际磁盘状态判定，不信任标志位。

    可能的现场（崩溃窗口内 replace 原子，内容恰在三处之一）：
    backup 在 → 放回 final（覆盖可能已装上的新内容）；
    backup 不在且 final 是登记过的旧身份 → 从未被触碰；
    backup 不在、final 是新内容/消失 → 唯一副本已丢，保全报错。
    """
    final = str(entry.get("final") or "")
    staged = entry.get("staged")
    backup = entry.get("backup")
    existed = bool(entry.get("existed", backup is not None))
    old_identity = _norm_identity(entry.get("old_identity"))

    if backup and os.path.lexists(str(backup)):
        os.replace(str(backup), final)          # 唯一旧副本从备份放回
        if not identity_matches(final, old_identity):
            raise _EntryRestoreError(
                OUTCOME_CONFLICT,
                f"restored backup identity mismatch: {final}")
        return
    if os.path.lexists(final):
        if not existed:
            # 新建条目：final 是本事务装入的新文件（replace 已吞掉 staged）
            if staged is None or not os.path.lexists(str(staged)):
                os.remove(final)
                return
            raise _EntryRestoreError(          # staged 还在＝final 非本事务
                OUTCOME_CONFLICT,               # 所装，不删别人的文件
                f"unexpected target exists: {final}")
        if identity_matches(final, old_identity):
            return                              # 旧文件从未被触碰
        raise _EntryRestoreError(
            OUTCOME_CONFLICT,
            f"new content installed but backup missing: {final}")
    if staged is not None and os.path.lexists(str(staged)):
        return                                  # 新内容仍在 staging，目标未动
    if not existed:
        return                                  # 新建条目从未安装
    raise _EntryRestoreError(
        OUTCOME_PENDING,
        f"sole copy unaccounted: {final}")


def recover_still_transactions(target_dir: str) -> List[str]:
    """清扫目标目录中遗留的事务目录，按 manifest 收敛到一致状态。

    返回**成功收敛**的 txn_id 列表（诊断用）；保全/隔离的不在其中，
    明细见 :func:`recover_still_transactions_detailed`。
    """
    return [r.txn_id for r in
            recover_still_transactions_detailed(target_dir)
            if r.converged]


def recover_still_transactions_detailed(
        target_dir: str) -> List[TxnRecoveryResult]:
    """逐事务恢复（BR01）：recovered／pending／conflict／quarantined。

    - 目录互斥忙（同进程活跃抓帧）→ 整轮跳过，不动在场事务；
    - journal 归属其他存活进程 → skipped_active；
    - manifest 不可读／schema 未知／条目路径越界 → quarantined（保全）；
    - uncommitted（prepared/backing/installing）→ 按意图×实际状态×身份
      放回旧集合，全部收敛且后置条件验证通过才清理；
    - committed → 验证新集合在位后清 staging（备份随之丢弃）；缺件
      则保全（conflict）；
    - rolled_back → 已收敛过，直接清理残留目录。
    """
    results: List[TxnRecoveryResult] = []
    if not os.path.isdir(target_dir):
        return results
    with try_recover_lock(target_dir) as lock:
        if not lock.acquired:
            return results      # 活跃事务在场：它自己管理自己的事务目录
        for name in sorted(os.listdir(target_dir)):
            if not name.startswith(TXN_DIR_PREFIX):
                continue
            txn_dir = os.path.join(target_dir, name)
            if not os.path.isdir(txn_dir):
                continue
            results.append(_recover_one_still_txn(txn_dir, name, target_dir))
        _log_still_results(target_dir, results)
    return results


def _recover_one_still_txn(txn_dir: str, name: str,
                           target_dir: str) -> TxnRecoveryResult:
    txn_id = name[len(TXN_DIR_PREFIX):]
    try:
        manifest = read_journal(_manifest_path(txn_dir))
    except JournalError as exc:
        return TxnRecoveryResult(txn_id, txn_dir, OUTCOME_QUARANTINED,
                                 str(exc))
    txn_id = str(manifest.get("txn_id") or txn_id)
    if other_process_alive(manifest.get("pid")):
        return TxnRecoveryResult(txn_id, txn_dir, "skipped_active",
                                 f"pid {manifest.get('pid')} still running")
    state = str(manifest.get("state") or "")
    entries = [e for e in manifest.get("entries", [])
               if isinstance(e, dict)]
    if not _still_entries_valid(entries, txn_dir, target_dir):
        return TxnRecoveryResult(txn_id, txn_dir, OUTCOME_QUARANTINED,
                                 "entry path escapes target/txn dirs")

    if state == STATE_ROLLED_BACK:
        shutil.rmtree(txn_dir, ignore_errors=True)
        return TxnRecoveryResult(txn_id, txn_dir, OUTCOME_RECOVERED,
                                 "rolled_back leftover cleaned")
    if state == STATE_COMMITTED:
        missing = [str(e["final"]) for e in entries
                   if e.get("staged") is not None
                   and not os.path.lexists(str(e["final"]))]
        if missing:
            return TxnRecoveryResult(
                txn_id, txn_dir, OUTCOME_CONFLICT,
                f"committed but finals missing: {missing}")
        shutil.rmtree(txn_dir, ignore_errors=True)
        return TxnRecoveryResult(txn_id, txn_dir, OUTCOME_RECOVERED,
                                 "committed set verified")
    if state not in _UNCOMMITTED_STATES:
        return TxnRecoveryResult(txn_id, txn_dir, OUTCOME_QUARANTINED,
                                 f"unknown state: {state!r}")
    if state == STATE_PREPARED:
        # prepared：目标从未被触碰（意图先行保证），只清 staging。
        shutil.rmtree(txn_dir, ignore_errors=True)
        return TxnRecoveryResult(txn_id, txn_dir, OUTCOME_RECOVERED,
                                 "prepared leftover cleaned")
    try:
        for entry in reversed(entries):
            _restore_entry_to_old_set(entry)
    except _EntryRestoreError as exc:
        return TxnRecoveryResult(txn_id, txn_dir, exc.outcome, str(exc))
    except OSError as exc:
        return TxnRecoveryResult(txn_id, txn_dir, OUTCOME_PENDING, str(exc))
    # 后置条件：旧集合完整在位（existed 条目 final 在且身份一致）。
    for entry in entries:
        final = str(entry.get("final") or "")
        if entry.get("existed") and not identity_matches(
                final, _norm_identity(entry.get("old_identity"))):
            return TxnRecoveryResult(
                txn_id, txn_dir, OUTCOME_CONFLICT,
                f"post-condition failed: {final}")
    shutil.rmtree(txn_dir, ignore_errors=True)
    return TxnRecoveryResult(txn_id, txn_dir, OUTCOME_RECOVERED,
                             "old set restored")


def _still_entries_valid(entries: List[Dict[str, Any]], txn_dir: str,
                         target_dir: str) -> bool:
    for entry in entries:
        final = entry.get("final")
        if not isinstance(final, str) or not path_within(final, target_dir):
            return False
        for key in ("staged", "backup"):
            value = entry.get(key)
            if value is not None and (
                    not isinstance(value, str)
                    or not path_within(value, txn_dir)):
                return False
    return True


def _log_still_results(target_dir: str,
                       results: List[TxnRecoveryResult]) -> None:
    if not results:
        return
    for result in results:
        logger.warning("still capture: recovery %s for %s in %s (%s)",
                       result.outcome, result.txn_id, target_dir,
                       result.detail)
    recovered = sum(1 for r in results if r.converged)
    logger.warning("still capture: %d/%d leftover transaction(s) "
                   "converged in %s", recovered, len(results), target_dir)


class StillCaptureTransaction:
    """单次静帧抓取的多文件提交事务。

    用法::

        txn = StillCaptureTransaction(preset_folder)   # 顺手恢复遗留事务
        staged_topos = txn.staged_path(topos_path)
        ...把 .topos/sidecar 写进 staged（同 basename）...
        txn.add(thumb_final_path)                       # 登记已写入的新文件
        txn.drop(stale_exr_final_path)                  # 本次集合没有的旧件
        txn.commit()                                    # 备份→安装→清理
        # 异常路径：rollback() 或上下文管理器退出时自动回滚；
        # 回滚单项失败时保留事务目录与备份（journal 供重试/人工恢复）。
    """

    def __init__(self, target_dir: str, *, txn_id: Optional[str] = None,
                 recover_leftovers: bool = True) -> None:
        self.target_dir = os.path.abspath(target_dir)
        self.txn_id = txn_id or uuid.uuid4().hex
        os.makedirs(self.target_dir, exist_ok=True)
        # 目录互斥：同目录并发抓帧明确失败，而不是互相破坏 staging。
        try:
            self._lock_keys = acquire_directories(self.target_dir)
        except DirectoryBusyError as exc:
            raise StillCaptureTransactionError(
                f"target busy (another capture in flight): {exc}") from exc
        if recover_leftovers:
            recover_still_transactions(self.target_dir)
        # staging 必须与最终目标同卷（单文件 rename 原子），故建在目标
        # 目录内；"." 前缀让 Gallery/扫描默认忽略。
        self.txn_dir = tempfile.mkdtemp(
            prefix=TXN_DIR_PREFIX, dir=self.target_dir)
        self.staged_dir = os.path.join(self.txn_dir, _STAGED_SUBDIR)
        self.backup_dir = os.path.join(self.txn_dir, _BACKUP_SUBDIR)
        os.makedirs(self.staged_dir)
        os.makedirs(self.backup_dir)
        self._entries: List[Dict[str, Any]] = []
        self._state = STATE_PREPARED
        self._write_manifest()

    # ── staging 阶段（现有目标不动） ─────────────────────────────────

    def staged_path(self, final_path: str) -> str:
        """最终目标路径 → staging 内同 basename 的写入路径。"""
        return os.path.join(self.staged_dir, os.path.basename(final_path))

    def add(self, final_path: str) -> None:
        """登记一个已写入 staging 的新文件（提交时安装到 final）。"""
        self._assert_state(STATE_PREPARED, "add")
        final = os.path.abspath(final_path)
        staged = self.staged_path(final)
        if not os.path.isfile(staged):
            raise StillCaptureTransactionError(
                f"staged file missing: {staged}")
        self._append_entry({
            "final": final, "staged": staged,
            "existed": os.path.lexists(final),
            "old_identity": list(file_identity(final))
            if os.path.lexists(final) else None,
        })

    def drop(self, final_path: str) -> None:
        """登记一个本次集合不包含的目标：提交后移除，回滚时恢复。"""
        self._assert_state(STATE_PREPARED, "drop")
        final = os.path.abspath(final_path)
        self._append_entry({
            "final": final, "staged": None,
            "existed": os.path.lexists(final),
            "old_identity": list(file_identity(final))
            if os.path.lexists(final) else None,
        })

    # ── 提交／回滚 ────────────────────────────────────────────────────

    def commit(self) -> None:
        """备份旧集合 → 安装新集合。任何一步失败立即回滚并重抛。"""
        self._assert_state(STATE_PREPARED, "commit")
        try:
            try:
                self._commit_backing()
                self._commit_installing()
            except Exception:
                self.rollback()
                raise
            # 提交完成后、清理前进程退出：manifest 必须已写成 committed，
            # recovery 才不会把已就位的新集合误判为中断而回滚。
            self._state = STATE_COMMITTED
            self._write_manifest()
            self.cleanup()
        finally:
            self._release_locks()

    def rollback(self) -> None:
        """恢复旧集合并清理 staging（幂等；committed 后只清理）。

        单项恢复失败：保留事务目录与备份，重抛原始 OSError——下一次
        :func:`recover_still_transactions` 或显式重试继续收敛，绝不删
        唯一旧副本。
        """
        if self._state in (STATE_COMMITTED, STATE_ROLLED_BACK):
            try:
                self.cleanup()
            finally:
                self._release_locks()
            return
        first_error: Optional[BaseException] = None
        try:
            for entry in reversed(self._entries):
                try:
                    _restore_entry_to_old_set(entry)
                except _EntryRestoreError as exc:
                    raise OSError(str(exc)) from exc
                except OSError as exc:
                    if first_error is None:
                        first_error = exc
        finally:
            if first_error is not None:
                self._release_locks()   # 现场保全：journal/backup 原地留存
        if first_error is not None:
            raise first_error
        self._state = STATE_ROLLED_BACK
        self._write_manifest()
        fsync_dir(self.target_dir)
        try:
            self.cleanup()
        finally:
            self._release_locks()

    def cleanup(self) -> None:
        """删除本事务的 staging 目录（含备份；只碰自己拥有的路径）。"""
        shutil.rmtree(self.txn_dir, ignore_errors=True)

    # ── 上下文管理器：异常退出自动回滚 ────────────────────────────────

    def __enter__(self) -> "StillCaptureTransaction":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._state not in (STATE_COMMITTED, STATE_ROLLED_BACK):
            self.rollback()
        else:
            self._release_locks()
        return False

    def __del__(self) -> None:  # 防御：调用方既不 commit 也不 rollback
        try:
            self._release_locks()
        except Exception:
            pass

    # ── 内部 ──────────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state

    def _assert_state(self, expected: str, op: str) -> None:
        if self._state != expected:
            raise StillCaptureTransactionError(
                f"{op} requires state {expected}, got {self._state}")

    def _append_entry(self, entry: Dict[str, Any]) -> None:
        entry.update({"backup": None, "backed_up": False,
                      "installed": False})
        if any(e["final"] == entry["final"] for e in self._entries):
            raise StillCaptureTransactionError(
                f"duplicate entry: {entry['final']}")
        self._entries.append(entry)
        self._write_manifest()

    def _commit_backing(self) -> None:
        self._state = STATE_BACKING
        self._write_manifest()
        for entry in self._entries:
            if not os.path.lexists(str(entry["final"])):
                continue
            backup = os.path.join(
                self.backup_dir, os.path.basename(str(entry["final"])))
            # 意图先行：备份归属先落盘，replace 之后的崩溃窗口可恢复。
            entry["backup"] = backup
            self._write_manifest()
            os.replace(str(entry["final"]), backup)
            entry["backed_up"] = True
            self._write_manifest()
        for entry in self._entries:
            if entry["staged"]:
                fsync_file(str(entry["staged"]))
        fsync_dir(self.backup_dir)

    def _commit_installing(self) -> None:
        self._state = STATE_INSTALLING
        self._write_manifest()
        for entry in self._entries:
            if entry["staged"] is None:
                continue  # drop 条目：旧件已在 backing 阶段移入备份
            os.replace(str(entry["staged"]), str(entry["final"]))
            entry["installed"] = True
            self._write_manifest()
        fsync_dir(self.target_dir)

    def _write_manifest(self) -> None:
        write_journal(_manifest_path(self.txn_dir), {
            "schema": TXN_JOURNAL_SCHEMA,
            "txn_id": self.txn_id,
            "target_dir": self.target_dir,
            "state": self._state,
            "entries": [dict(e) for e in self._entries],
            **make_owner_block(),
        })

    def _release_locks(self) -> None:
        keys = getattr(self, "_lock_keys", None)
        if keys:
            self._lock_keys = []
            release_directories(keys)
