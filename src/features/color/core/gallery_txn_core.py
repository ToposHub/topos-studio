"""Gallery／静帧文件事务共享协议（BR01）——先落盘意图，后动文件。

2026-11-10 复查（docs/plans/2026-11-10-color-gallery-backend-review-followup.md
BR01）发现三套相似事务（still capture／gallery bundle move／bundle import）
共享同一类缺陷：物理 rename 先于 journal 登记（崩溃窗口丢唯一副本）、
恢复无条件删事务目录、坏日志驱动库外文件操作、无并发互斥。本模块把
共同纪律提炼为单一实现，各事务模块只保留领域语义：

- **意图先行**：任何文件动作之前，journal 已登记该动作涉及的全部路径
  （src/staged/dst/backup）与旧内容身份（size+mtime_ns）；
- **原子日志**：tmp＋fsync＋replace＋目录 fsync；
- **实际状态收敛**：恢复按"journal 意图 × 磁盘实际状态 × 内容身份"判定，
  不依赖可能滞后的 installed/backed_up 布尔值；
- **保全优先**：恢复失败／未知状态／坏 JSON／未知 schema／库外路径一律
  保留事务目录与唯一副本（outcome＝pending/conflict/quarantined），只有
  全部后置条件验证成功才清理；
- **目录互斥**：同进程内对目标目录的非阻塞锁把并发 capture/import/
  rename 与自动恢复隔开；跨进程以 OS 级租约（flock，进程内引用计数
  共享）互斥，journal 的 pid 活性检查只兜底清扫。
- **活跃事务登记**（SR01）：同线程重入（模态回调/脚本重入）可再次
  拿到目录锁，此时"日志 pid==本进程"不再等于"可恢复"——进程内
  登记表 :data:`ACTIVE_TXNS` 才是活跃所有者的权威，恢复对已登记的
  事务目录一律 skipped_active。
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:  # POSIX 跨进程租约；缺失时退化为无租约（同进程锁仍有效）
    import fcntl
except ImportError:  # pragma: no cover - 平台差异
    fcntl = None

__all__ = [
    "TXN_JOURNAL_SCHEMA",
    "OUTCOME_RECOVERED",
    "OUTCOME_PENDING",
    "OUTCOME_CONFLICT",
    "OUTCOME_QUARANTINED",
    "OUTCOME_SKIPPED_ACTIVE",
    "TxnRecoveryResult",
    "DirectoryBusyError",
    "JournalError",
    "ActiveTxnRegistry",
    "ACTIVE_TXNS",
    "read_journal",
    "write_journal",
    "fsync_file",
    "fsync_dir",
    "silent_remove",
    "file_identity",
    "identity_matches",
    "path_within",
    "make_owner_block",
    "other_process_alive",
    "acquire_directories",
    "release_directories",
    "try_recover_lock",
]

#: journal schema（结构变更必须 bump 并在恢复侧拒识未知版本）
TXN_JOURNAL_SCHEMA = 1

# 逐事务恢复结局（稳定键，诊断/UI 共用）
OUTCOME_RECOVERED = "recovered"          # 已收敛（旧集合或新集合完整就位）
OUTCOME_PENDING = "pending"              # 设备/权限暂时不可用：保留待重试
OUTCOME_CONFLICT = "conflict"            # 磁盘状态与日志矛盾：保留待人工
OUTCOME_QUARANTINED = "quarantined"      # 坏日志/未知 schema/库外路径：保全不动
OUTCOME_SKIPPED_ACTIVE = "skipped_active"  # 其他进程仍在执行：不动


@dataclass(frozen=True)
class TxnRecoveryResult:
    """单个遗留事务的恢复结论（含诊断位置，供 UI/日志呈现）。"""

    txn_id: str
    txn_dir: str
    outcome: str
    detail: str = ""

    @property
    def converged(self) -> bool:
        return self.outcome == OUTCOME_RECOVERED


class DirectoryBusyError(RuntimeError):
    """目标目录正被同进程另一个事务占用（排队/拒绝由调用方定）。"""


class JournalError(ValueError):
    """journal 不可读／结构非法（恢复侧必须保全，不得据此动文件）。"""


# ── 低层文件原语 ────────────────────────────────────────────────────────

#: 平台确实不支持 fsync 的 errno（其余 I/O 错误必须上抛，SR02.6——
#: 需要持久化保证的场景不能把真实 I/O 失败当成"平台不支持"吞掉）。
_FSYNC_UNSUPPORTED = frozenset({
    errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS,
})


def fsync_dir(path: str) -> None:
    """目录条目落盘。

    目录本身消失（并发清理）时无事可做；平台不支持时静默；其余
    ``OSError``（EIO/ENOSPC/…）上抛——持久化失败的路径必须停下来。
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in _FSYNC_UNSUPPORTED:
            raise
    finally:
        os.close(fd)


def fsync_file(path: str) -> None:
    """文件内容落盘（打不开＝出错，上抛；不支持 fsync 的平台除外）。"""
    with open(path, "rb") as fh:
        try:
            os.fsync(fh.fileno())
        except OSError as exc:
            if exc.errno not in _FSYNC_UNSUPPORTED:
                raise


def silent_remove(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def write_journal(path: str, payload: Dict[str, Any]) -> None:
    """原子写 journal：tmp＋fsync＋replace＋目录 fsync。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    fsync_dir(os.path.dirname(path) or ".")


def read_journal(path: str) -> Dict[str, Any]:
    """读并校验 journal。

    raises:
        JournalError: 文件缺失／JSON 损坏／非对象／schema 未知。
        （缺失 schema 视为遗留版本 1——旧日志可确认则迁移，见各模块。）
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as exc:
        raise JournalError(f"unreadable journal: {exc}") from exc
    if not isinstance(payload, dict):
        raise JournalError("journal is not a JSON object")
    schema = payload.get("schema", TXN_JOURNAL_SCHEMA)  # 遗留日志无版本号
    if type(schema) is not int or schema != TXN_JOURNAL_SCHEMA:
        raise JournalError(f"unsupported journal schema: {schema!r}")
    for field, member_types in (("entries", (dict,)), ("finals", (str, dict))):
        if field not in payload:
            continue
        members = payload[field]
        if not isinstance(members, list) or any(
                not isinstance(member, member_types) for member in members):
            raise JournalError(f"invalid journal {field}")
    return payload


# ── 内容身份与路径归属 ──────────────────────────────────────────────────

def file_identity(path: str) -> Optional[Tuple[int, int]]:
    """轻量内容身份（size, mtime_ns）；os.replace 两侧保持不变。"""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


def identity_matches(path: str,
                     identity: Optional[Tuple[int, int]]) -> bool:
    """path 当前内容是否仍是登记过的身份（无登记＝无法证伪＝通过）。"""
    if identity is None:
        return True
    return file_identity(path) == tuple(identity)


def path_within(path: str, root: str) -> bool:
    """realpath 归属校验：坏日志不得驱动库外文件操作。"""
    real = os.path.realpath(os.path.abspath(path))
    root_real = os.path.realpath(os.path.abspath(root))
    return real == root_real or real.startswith(root_real + os.sep)


# ── 进程所有者 ──────────────────────────────────────────────────────────

def make_owner_block() -> Dict[str, Any]:
    return {"pid": os.getpid()}


def other_process_alive(pid: Any) -> bool:
    """journal 归属的其他进程是否仍在执行（本进程 pid 恒 False）。"""
    if type(pid) is not int or pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # EPERM 等：进程存在但非本用户所有
    return True


# ── 活跃事务登记（SR01：进程内所有者身份） ─────────────────────────────

class ActiveTxnRegistry:
    """进程内活跃事务目录登记表。

    RLock 只表达跨线程互斥；同线程的第二笔事务（模态回调、脚本重入）
    可以重入持锁，此时"journal pid==本进程"**不再**等于"可恢复"——
    本表才是进程内活跃所有者的权威。恢复入口对已登记的事务目录必须
    返回 skipped_active，绝不把在场事务的 staging 当遗留清扫。
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._active: Dict[str, int] = {}   # realpath(txn_dir) → 引用计数

    def register(self, txn_dir: str) -> str:
        """登记一个活跃事务目录；返回 token 交给 :meth:`unregister`。"""
        key = os.path.realpath(os.path.abspath(txn_dir))
        with self._guard:
            self._active[key] = self._active.get(key, 0) + 1
        return key

    def unregister(self, token: Optional[str]) -> None:
        if not token:
            return
        with self._guard:
            count = self._active.get(token)
            if count is None:
                return
            if count <= 1:
                del self._active[token]
            else:
                self._active[token] = count - 1

    def is_active(self, txn_dir: str) -> bool:
        key = os.path.realpath(os.path.abspath(txn_dir))
        with self._guard:
            return key in self._active


#: 进程级单例：三套事务（capture/move/import）共用。
ACTIVE_TXNS = ActiveTxnRegistry()


# ── 目录互斥（跨线程 RLock＋跨进程租约；SR01） ─────────────────────────

def _lease_root() -> str:
    base = os.path.join(tempfile.gettempdir(), "topos-dir-leases")
    os.makedirs(base, exist_ok=True)
    return base


class _DirLeaseTable:
    """进程级跨进程目录租约（flock 共享文件锁）。

    同一进程对同一目录的多次获取只持有一把 OS 锁（引用计数）——同线程
    重入不被自己挡住；另一进程持有则 :class:`DirectoryBusyError`。
    进程退出（含崩溃）由 OS 自动释放。锁文件是系统临时目录下的小空
    文件，不落入用户画廊。
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._leases: Dict[str, List[int]] = {}   # key → [fd, count]

    def _lock_fd(self, fd: int, key: str) -> None:
        if fcntl is None:
            return                       # 无租约原语的平台：退化为无租约
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise DirectoryBusyError(
                f"{key}: held by another process "
                f"({exc.strerror or exc})") from exc

    def acquire(self, key: str) -> None:
        with self._guard:
            entry = self._leases.get(key)
            if entry is not None:
                entry[1] += 1
                return
            path = os.path.join(
                _lease_root(),
                hashlib.sha1(key.encode("utf-8", "surrogateescape")
                             ).hexdigest() + ".lock")
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                self._lock_fd(fd, key)
            except BaseException:
                os.close(fd)
                raise
            self._leases[key] = [fd, 1]

    def release(self, key: str) -> None:
        with self._guard:
            entry = self._leases.get(key)
            if entry is None:
                return                    # 防御：未获取的释放请求
            entry[1] -= 1
            if entry[1] > 0:
                return
            del self._leases[key]
            fd = entry[0]
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass


_LEASES = _DirLeaseTable()


class _DirLockTable:
    """同进程目录锁表（跨线程 RLock；引用计数清零即回收，不无限保留）。"""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: Dict[str, List[Any]] = {}   # key → [RLock, holders]

    def acquire(self, key: str) -> threading.RLock:
        with self._guard:
            entry = self._locks.get(key)
            if entry is None:
                entry = [threading.RLock(), 0]
                self._locks[key] = entry
            return entry[0]

    def retain(self, key: str) -> None:
        with self._guard:
            self._locks[key][1] += 1

    def release_ref(self, key: str) -> None:
        with self._guard:
            entry = self._locks.get(key)
            if entry is None:
                return
            entry[1] -= 1
            if entry[1] <= 0:
                del self._locks[key]     # 无使用者的目录对象及时回收

    def size(self) -> int:
        with self._guard:
            return len(self._locks)


_TABLE = _DirLockTable()


def acquire_directories(*dirs: str) -> List[str]:
    """按 realpath 排序非阻塞获取目录锁（防 AB-BA）；忙则整体失败。

    两层互斥（SR01）：

    - 跨线程：进程内 RLock（同线程重入合法——恢复与业务事务可嵌套）；
    - 跨进程：系统临时目录下的 flock 租约（同进程引用计数共享一把）。

    returns: 已获取的目录键列表（排序后），交给 :func:`release_directories`。
    raises: DirectoryBusyError——一个都没拿（部分获取全部回退）。
    """
    keys = sorted({os.path.realpath(os.path.abspath(d)) for d in dirs if d})
    taken_locks: List[str] = []
    taken_leases: List[str] = []
    for key in keys:
        lock = _TABLE.acquire(key)
        if not lock.acquire(blocking=False):
            for held in reversed(taken_locks):
                _TABLE.acquire(held).release()
                _TABLE.release_ref(held)
            for held in reversed(taken_leases):
                _LEASES.release(held)
            raise DirectoryBusyError(key)
        taken_locks.append(key)
        _TABLE.retain(key)
        try:
            _LEASES.acquire(key)
        except DirectoryBusyError:
            for held in reversed(taken_locks):
                _TABLE.acquire(held).release()
                _TABLE.release_ref(held)
            for held in reversed(taken_leases):
                _LEASES.release(held)
            raise
        taken_leases.append(key)
    return keys


def release_directories(keys: Sequence[str]) -> None:
    for key in keys:
        try:
            _TABLE.acquire(key).release()
        except RuntimeError:
            pass  # 深度失衡防御：宁可放过不可炸恢复路径
        finally:
            _LEASES.release(key)
            _TABLE.release_ref(key)


class try_recover_lock:
    """恢复入口的目录锁上下文：忙＝有活跃事务在场，跳过整轮恢复。"""

    def __init__(self, *dirs: str) -> None:
        self._keys: Optional[List[str]] = None
        self._dirs = dirs

    def __enter__(self) -> "try_recover_lock":
        try:
            self._keys = acquire_directories(*self._dirs)
        except DirectoryBusyError:
            self._keys = None
        return self

    @property
    def acquired(self) -> bool:
        return self._keys is not None

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._keys is not None:
            release_directories(self._keys)
            self._keys = None
        return False
