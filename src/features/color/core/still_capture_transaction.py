"""R01：静帧抓取的多文件保存事务（staging → 备份 → 安装 → 提交）。

缺口（2026-11-09 复查 R01）：``on_capture_still`` 先把 sidecar 直接写
到最终目标、再让 SaveRouter 保存 ``.topos`` 本体；失败分支按固定目标
名删文件——覆盖保存失败时旧 sidecar 已被新内容破坏又被删除，留下的
旧 ``.topos`` 变成不完整预设（无缩略图/无参考帧/过期元数据）。

契约（:class:`StillCaptureTransaction`）：

- 一次抓帧 = 一个事务：新文件全部先写入目标目录内的隐藏 staging 目录
  （同卷，单文件 ``os.replace`` 原子），现有目标在提交前一律不动；
- 提交按 manifest 日志分阶段推进（prepared → backing → installing →
  committed），每次状态/条目变更先落盘再动文件；中途异常立即回滚，
  进程退出的遗留事务由 :func:`recover_still_transactions` 在下一次事务
  开始时按 manifest 把整个旧文件集合原样放回；
- 只清理本事务拥有的 staging 目录，禁止按固定目标名删除既有资产；
  旧资产一律先备份再替换，回滚按字节原样恢复；
- 本次集合不包含的旧 sidecar（如降级未写 EXR）按 ``drop`` 条目处理：
  提交后不保留过期副本，回滚时照常恢复。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import uuid
from typing import Any, Dict, List, Optional

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


class StillCaptureTransactionError(RuntimeError):
    """事务推进失败（回滚已执行或 staging 状态不满足前置条件）。"""


def _fsync_dir(path: str) -> None:
    """目录条目落盘（best-effort；平台不支持时静默）。"""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _fsync_file(path: str) -> None:
    try:
        with open(path, "rb") as fh:
            os.fsync(fh.fileno())
    except OSError:
        pass


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(os.path.dirname(path))


def _manifest_path(txn_dir: str) -> str:
    return os.path.join(txn_dir, MANIFEST_NAME)


def _read_manifest(txn_dir: str) -> Optional[Dict[str, Any]]:
    try:
        with open(_manifest_path(txn_dir), "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None


def _restore_entries(entries: List[Dict[str, Any]]) -> None:
    """按 manifest 恢复旧集合：撤掉已安装的新文件，放回全部备份。

    与提交顺序对称（逆序恢复）；条目只在 manifest 登记过的范围内动作，
    未登记的文件不碰。目标目录以条目自身路径为准。
    """
    target_dir: Optional[str] = None
    for entry in reversed(entries):
        if entry.get("installed"):
            _silent_remove(str(entry["final"]))
        backup = entry.get("backup")
        if backup and os.path.isfile(str(backup)):
            os.replace(str(backup), str(entry["final"]))
            target_dir = os.path.dirname(str(entry["final"]))
    if target_dir:
        _fsync_dir(target_dir)


def recover_still_transactions(target_dir: str) -> List[str]:
    """清扫目标目录中遗留的事务目录，按 manifest 收敛到一致状态。

    返回恢复过的 txn_id 列表（诊断用）。各状态的收敛动作：

    - 无 manifest／prepared：目标未被触碰 → 删除 staging；
    - backing／installing：按 manifest 把旧文件集合原样放回，再删 staging；
    - committed：新集合已就位 → 只删 staging（备份随之丢弃）。
    """
    recovered: List[str] = []
    if not os.path.isdir(target_dir):
        return recovered
    for name in sorted(os.listdir(target_dir)):
        if not name.startswith(TXN_DIR_PREFIX):
            continue
        txn_dir = os.path.join(target_dir, name)
        if not os.path.isdir(txn_dir):
            continue
        manifest = _read_manifest(txn_dir)
        if manifest is None:
            shutil.rmtree(txn_dir, ignore_errors=True)
            continue
        recovered.append(str(manifest.get("txn_id", name)))
        if manifest.get("state") in (STATE_BACKING, STATE_INSTALLING):
            entries = [e for e in manifest.get("entries", [])
                       if isinstance(e, dict)]
            _restore_entries(entries)
        shutil.rmtree(txn_dir, ignore_errors=True)
    if recovered:
        logger.warning(
            "still capture: recovered %d leftover transaction(s) in %s: %s",
            len(recovered), target_dir, recovered)
    return recovered


class StillCaptureTransaction:
    """单次静帧抓取的多文件提交事务。

    用法::

        txn = StillCaptureTransaction(preset_folder)   # 顺手恢复遗留事务
        staged_topos = txn.staged_path(topos_path)
        ...把 .topos/sidecar 写进 staged（同 basename）...
        txn.add(thumb_final_path)                       # 登记已写入的新文件
        txn.drop(stale_exr_final_path)                  # 本次集合没有的旧件
        txn.commit()                                    # 备份→安装→清理
        # 异常路径：rollback() 或上下文管理器退出时自动回滚
    """

    def __init__(self, target_dir: str, *, txn_id: Optional[str] = None,
                 recover_leftovers: bool = True) -> None:
        self.target_dir = os.path.abspath(target_dir)
        self.txn_id = txn_id or uuid.uuid4().hex
        if recover_leftovers:
            recover_still_transactions(self.target_dir)
        os.makedirs(self.target_dir, exist_ok=True)
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
        self._append_entry({"final": final, "staged": staged})

    def drop(self, final_path: str) -> None:
        """登记一个本次集合不包含的目标：提交后移除，回滚时恢复。"""
        self._assert_state(STATE_PREPARED, "drop")
        self._append_entry({"final": os.path.abspath(final_path),
                            "staged": None})

    # ── 提交／回滚 ────────────────────────────────────────────────────

    def commit(self) -> None:
        """备份旧集合 → 安装新集合。任何一步失败立即回滚并重抛。"""
        self._assert_state(STATE_PREPARED, "commit")
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

    def rollback(self) -> None:
        """恢复旧集合并清理 staging（幂等；committed 后只清理）。"""
        if self._state in (STATE_COMMITTED, STATE_ROLLED_BACK):
            self.cleanup()
            return
        try:
            _restore_entries(self._entries)
        finally:
            self._state = STATE_ROLLED_BACK
            self.cleanup()

    def cleanup(self) -> None:
        """删除本事务的 staging 目录（含备份；只碰自己拥有的路径）。"""
        shutil.rmtree(self.txn_dir, ignore_errors=True)

    # ── 上下文管理器：异常退出自动回滚 ────────────────────────────────

    def __enter__(self) -> "StillCaptureTransaction":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._state not in (STATE_COMMITTED, STATE_ROLLED_BACK):
            self.rollback()
        return False

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
            os.replace(str(entry["final"]), backup)
            entry["backup"] = backup
            entry["backed_up"] = True
            self._write_manifest()
        for entry in self._entries:
            if entry["staged"]:
                _fsync_file(str(entry["staged"]))
        _fsync_dir(self.backup_dir)

    def _commit_installing(self) -> None:
        self._state = STATE_INSTALLING
        self._write_manifest()
        for entry in self._entries:
            if entry["staged"] is None:
                continue  # drop 条目：旧件已在 backing 阶段移入备份
            os.replace(str(entry["staged"]), str(entry["final"]))
            entry["installed"] = True
            self._write_manifest()
        _fsync_dir(self.target_dir)

    def _write_manifest(self) -> None:
        _atomic_write_json(_manifest_path(self.txn_dir), {
            "schema": 1,
            "txn_id": self.txn_id,
            "target_dir": self.target_dir,
            "state": self._state,
            "entries": [dict(e) for e in self._entries],
        })
