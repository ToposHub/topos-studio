"""F13：跨平台安装锁——等价原语、跨进程并发/异常退出语义一致、拒绝≠支持。

本机（POSIX）跑真实 flock 跨进程场景；Windows msvcrt 用等价替身锁定
重试/释放语义；无原语平台验证 LockUnavailable 的诚实拒绝。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from src.shared.plugins import install_lock
from src.shared.plugins.install_lock import (
    HeldTransactionLock,
    LockUnavailable,
    create_lock,
    platform_lock_kinds,
)

_LOCKER = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    sys.path.insert(0, {root!r})
    Path(sys.argv[1]).parent.mkdir(parents=True, exist_ok=True)
    from src.shared.plugins.install_lock import create_lock
    lock = create_lock(sys.argv[1])
    lock.acquire()
    print("HELD", flush=True)
    try:
        time.sleep(float(sys.argv[2]))
    finally:
        lock.close()
    print("RELEASED", flush=True)
""")


def _spawn_holder(lock_path: Path, hold_seconds: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-u", "-c", _LOCKER.format(root=os.getcwd()),
         str(lock_path), hold_seconds],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


# ---------------------------------------------------------------------------
# 平台选择与可用性事实
# ---------------------------------------------------------------------------

def test_platform_lock_kinds_reports_facts():
    facts = platform_lock_kinds()
    assert set(facts) == {"fcntl", "msvcrt"}
    assert facts["fcntl"] == (os.name == "posix")


def test_create_lock_selects_posix_flock_on_this_machine():
    if os.name != "posix":
        pytest.skip("非 POSIX 机器")
    lock = create_lock(Path("/tmp/any.lock"))
    assert type(lock).__name__ == "_FlockLock"


def test_no_primitive_platform_refuses_honestly(monkeypatch, tmp_path):
    """fcntl/msvcrt 都没有：LockUnavailable——对不支持平台的诚实拒绝。"""
    monkeypatch.setattr(install_lock, "fcntl", None)
    monkeypatch.setattr(install_lock, "msvcrt", None)
    with pytest.raises(LockUnavailable) as exc:
        create_lock(tmp_path / "t.lock")
    assert "拒绝≠支持" not in str(exc.value)  # 字面不写口号
    assert "不支持并发安装事务，拒绝执行" in str(exc.value)
    assert "fcntl/msvcrt 均不可用" in exc.value.detail


# ---------------------------------------------------------------------------
# POSIX 真跨进程：独占 / 阻塞等待 / kill -9 自动释放
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name != "posix", reason="POSIX 真锁场景")
def test_cross_process_exclusion_and_release(tmp_path):
    lock_path = tmp_path / "tx.lock"
    contender = None
    holder = _spawn_holder(lock_path, "1.5")
    try:
        assert holder.stdout.readline().strip() == "HELD"
        contender = _spawn_holder(lock_path, "0.1")
        import time as _t
        _t.sleep(0.4)
        # 独占：持有者未放行，竞争者仍在等锁（不得提前消费其输出）
        assert contender.poll() is None
        assert holder.stdout.readline().strip() == "RELEASED"   # 阻塞至释放
        out, _ = contender.communicate(timeout=10)
        assert "HELD" in out and "RELEASED" in out              # 随后获得
    finally:
        for proc in (holder, contender):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="POSIX 真锁场景")
def test_lock_auto_released_on_process_kill(tmp_path):
    """异常退出语义：持有者被 kill -9 → 内核释放锁 → 下一个进程可得。"""
    lock_path = tmp_path / "tx.lock"
    victim = _spawn_holder(lock_path, "30")
    try:
        assert victim.stdout.readline().strip() == "HELD"
        victim.kill()
        victim.wait(timeout=5)
        successor = _spawn_holder(lock_path, "0.1")
        out, _ = successor.communicate(timeout=10)
        assert "HELD" in out and "RELEASED" in out   # 无需人工清理即获得
    finally:
        for proc in (victim,):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


def test_installer_transaction_uses_platform_lock(tmp_path):
    """安装事务经新锁层：与独立持锁进程互斥（真跨进程独占）。"""
    import threading as _threading
    import time as _time
    from src.shared.plugins.installer import LocalPackageInstaller

    lock_path = tmp_path / "installed" / ".transactions.lock"
    holder = _spawn_holder(lock_path, "1.2")
    try:
        assert holder.stdout.readline().strip() == "HELD"
        installer = LocalPackageInstaller(
            tmp_path / "installed",
            host_facts=dict(host_api=1, topos_version="3.0.0",
                            platform="darwin", arch="x86_64"))
        outcomes: list = []
        thread = _threading.Thread(
            target=lambda: outcomes.append(
                installer.install(_make_pkg(tmp_path))),
            daemon=True)
        thread.start()
        _time.sleep(0.4)
        assert not outcomes                 # 持锁进程未放：安装仍在等锁
        assert holder.stdout.readline().strip() == "RELEASED"
        thread.join(timeout=10)
        assert outcomes and outcomes[0].outcome == "installed"
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)


def _make_pkg(tmp_path: Path) -> Path:
    folder = tmp_path / "pkg"
    folder.mkdir(exist_ok=True)
    (folder / "topos.plugin.json").write_text(
        "{\"plugin_id\": \"files.lockt\", \"display_name\": \"L\", "
        "\"version\": \"1.0.0\", \"capabilities\": []}", encoding="utf-8")
    return folder


# ---------------------------------------------------------------------------
# Windows msvcrt 等价原语：替身锁定重试/释放语义
# ---------------------------------------------------------------------------

class _FakeMsvcrt:
    """等价替身：LK_NBLCK 独占语义 + 持有表，验证重试循环与释放。"""

    LK_NBLCK = 1
    LK_UNLCK = 2
    locks: dict[int, bool] = {}

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def locking(self, fd, mode, nbytes):
        self.calls.append((fd, mode))
        if mode == self.LK_NBLCK:
            if self.locks.get(fd):
                raise OSError("locked by another")
            self.locks[fd] = True
            return
        if mode == self.LK_UNLCK:
            self.locks.pop(fd, None)


def test_msvcrt_lock_retry_until_holder_releases(monkeypatch, tmp_path):
    fake = _FakeMsvcrt()
    monkeypatch.setattr(install_lock, "fcntl", None)
    monkeypatch.setattr(install_lock, "msvcrt", fake)
    lock_path = tmp_path / "w.lock"
    lock = create_lock(lock_path)
    assert type(lock).__name__ == "_MsvcrtLock"
    # 预占：模拟另一进程持有（第一次 NBLCK 失败，随后释放）
    other_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fake.locks[other_fd] = True

    import threading as _threading
    released = _threading.Event()

    def free_soon():
        import time as _time
        _time.sleep(0.15)
        fake.locks.pop(other_fd, None)
        released.set()

    _threading.Thread(target=free_soon, daemon=True).start()
    lock.acquire()                                  # 阻塞重试直到可用
    assert released.wait(2)
    assert any(mode == fake.LK_NBLCK for _fd, mode in fake.calls)
    assert fake.locks.get(lock._fd) is True         # 现在由本锁持有
    lock.close()
    assert lock._fd is None                         # 句柄关闭
    os.close(other_fd)


def test_msvcrt_lock_release_idempotent(monkeypatch, tmp_path):
    fake = _FakeMsvcrt()
    monkeypatch.setattr(install_lock, "fcntl", None)
    monkeypatch.setattr(install_lock, "msvcrt", fake)
    lock = create_lock(tmp_path / "w2.lock")
    lock.acquire()
    lock.release()
    lock.release()                                  # 幂等
    lock.close()
    lock.close()                                    # 幂等


def test_msvcrt_lock_unavailable_when_module_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(install_lock, "fcntl", None)
    monkeypatch.setattr(install_lock, "msvcrt", None)
    with pytest.raises(LockUnavailable):
        create_lock(tmp_path / "w3.lock")


# ---------------------------------------------------------------------------
# 组合持有（线程锁 + 跨进程锁）契约
# ---------------------------------------------------------------------------

def test_held_transaction_lock_reentrant_thread_semantics(tmp_path):
    import threading
    if os.name != "posix":
        pytest.skip("POSIX")
    thread_lock = threading.RLock()
    with HeldTransactionLock(thread_lock, tmp_path / "h.lock"):
        with thread_lock:                           # 线程锁可重入
            pass
    # 退出后可再次获取
    with HeldTransactionLock(thread_lock, tmp_path / "h.lock"):
        pass
