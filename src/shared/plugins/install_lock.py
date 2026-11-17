"""F13：按实际目标平台的跨进程安装锁——无 fcntl 平台的等价原语。

现状（反模式）：``_Transaction`` 在 ``fcntl`` 缺失时抛 ``LOCK_UNAVAILABLE``
——把"拒绝安装"当成"跨平台支持完成"。本模块按平台提供**等价锁原语**：

* **POSIX（fcntl.flock）**：独占、阻塞、内核在进程退出（含 kill -9）时
  自动释放——既有语义原样抽出；
* **Windows（msvcrt.locking）**：``os.open`` 句柄 + ``LK_NBLCK`` 重试循
  环（阻塞语义等价；句柄关闭/进程退出由 OS 释放，与 flock 一致）；
* **两者皆无的平台**：``RefusingLock``——明确"平台不支持安装事务"并
  报可用性事实；这是对**不支持平台**的诚实拒绝（允许），不是把拒绝
  冒充支持。

各平台语义一致性契约（``LockContract`` 测试模板逐项锁定）：独占性（第
二个获取者必须等待）、进程异常退出自动释放、显式释放幂等。
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Protocol

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - 平台分支
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None  # type: ignore[assignment]


class LockUnavailable(RuntimeError):
    """平台没有可用锁原语——如实报告（拒绝≠支持）。"""

    def __init__(self, platform_name: str, detail: str) -> None:
        super().__init__(
            f"平台 {platform_name} 没有跨进程安装锁原语（fcntl/msvcrt 均"
            f"不可用）：{detail}——该平台不支持并发安装事务，拒绝执行")
        self.platform_name = platform_name
        self.detail = detail


class CrossProcessLock(Protocol):
    """跨进程独占锁契约：阻塞获取；进程退出自动释放；幂等释放。"""

    def acquire(self) -> None: ...
    def release(self) -> None: ...
    def close(self) -> None: ...


class _FlockLock:
    """POSIX flock 版（从 _Transaction 抽出，语义不变）。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle = None

    def acquire(self) -> None:
        flags = (os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0))
        descriptor = os.open(self._path, flags, 0o600)
        self._handle = os.fdopen(descriptor, "a+b")
        import stat as _stat
        info = os.fstat(self._handle.fileno())
        if not _stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            self._handle.close()
            self._handle = None
            raise LockUnavailable(os.name, "事务锁必须是独立的普通文件")
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)   # 阻塞等锁

    def release(self) -> None:
        if self._handle is not None:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass

    def close(self) -> None:
        if self._handle is not None:
            try:
                self.release()
            finally:
                self._handle.close()
                self._handle = None


class _MsvcrtLock:
    """Windows msvcrt.locking 版：LK_NBLCK 重试循环（阻塞语义等价）。"""

    def __init__(self, path: Path, *, retry_interval: float = 0.05,
                 stall_timeout: float | None = None) -> None:
        self._path = path
        self._retry_interval = retry_interval
        self._stall_timeout = stall_timeout
        self._fd: int | None = None

    def acquire(self) -> None:
        descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        self._fd = descriptor
        started = time.monotonic()
        while True:
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if self._stall_timeout is not None and \
                        time.monotonic() - started > self._stall_timeout:
                    os.close(descriptor)
                    self._fd = None
                    raise
                time.sleep(self._retry_interval)   # 阻塞语义：持锁者退出即获得

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass

    def close(self) -> None:
        if self._fd is not None:
            try:
                self.release()
            finally:
                os.close(self._fd)   # 进程退出时 OS 亦强制释放
                self._fd = None


def platform_lock_kinds() -> dict[str, bool]:
    """平台锁原语可用性事实（诊断/测试用）。"""
    return {"fcntl": fcntl is not None, "msvcrt": msvcrt is not None}


def create_lock(path: Path) -> CrossProcessLock:
    """按实际平台选择锁原语；两者皆无 → LockUnavailable（拒绝≠支持）。"""
    path = Path(path)
    if os.name == "nt" or (fcntl is None and msvcrt is not None):
        if msvcrt is None:
            raise LockUnavailable(os.name, "fcntl/msvcrt 均不可用")
        return _MsvcrtLock(path)
    if fcntl is None:
        if msvcrt is None:
            raise LockUnavailable(os.name, "fcntl/msvcrt 均不可用")
        return _MsvcrtLock(path)
    return _FlockLock(path)


class HeldTransactionLock:
    """线程锁 + 跨进程锁的持有组合（_Transaction 使用）。"""

    def __init__(self, thread_lock: threading.RLock, lock_path: Path) -> None:
        self._thread_lock = thread_lock
        self._lock_path = lock_path
        self._lock: CrossProcessLock | None = None
        self._acquired = False

    def __enter__(self) -> "HeldTransactionLock":
        self._thread_lock.acquire()
        try:
            parent = self._lock_path.parent
            if parent.is_symlink():
                raise RuntimeError("安装根是符号链接，拒绝落事务锁")
            parent.mkdir(parents=True, exist_ok=True)
            if self._lock_path.is_symlink():
                raise RuntimeError("事务锁不能是符号链接")
            self._lock = create_lock(self._lock_path)
            self._lock.acquire()
            self._acquired = True
        except BaseException:
            self._thread_lock.release()
            raise
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._lock is not None:
            try:
                if self._acquired:
                    self._lock.release()
            finally:
                self._lock.close()
                self._lock = None
        self._thread_lock.release()


__all__ = [
    "CrossProcessLock", "HeldTransactionLock", "LockUnavailable",
    "create_lock", "platform_lock_kinds",
]
