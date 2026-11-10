"""SR01：活跃事务身份与同线程／跨进程互斥验收。

对应《剪辑／调色 Gallery 后端第二轮复查计划》SR01（2026-11-10）：
- 同线程第二笔抓帧事务不再被自动恢复清掉在场 staging（登记表是
  进程内活跃所有者的权威，"日志 pid==本进程"不等于"可恢复"）；
- 恢复对已登记事务一律 skipped_active；所有者消亡（注销）后才可恢复；
- OS 级跨进程租约：另一进程持锁时本进程明确 busy，不互删 staging；
- 初始化任一步失败后锁/登记平衡释放，新请求不被卡死；
- 锁表引用计数清零即回收；fsync 只在"平台不支持"时静默，真实 I/O
  错误上抛（SR02.6 合同）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from src.features.color.core import gallery_txn_core as core
from src.features.color.core.still_capture_transaction import (
    StillCaptureTransaction,
    StillCaptureTransactionError,
    recover_still_transactions_detailed,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestSameThreadCoexistence:
    def test_second_capture_preserves_first_staging(self, tmp_path):
        """复现场景（repro SR01）：同线程第二笔事务不清在场 staging。"""
        first = StillCaptureTransaction(str(tmp_path),
                                        recover_leftovers=False)
        staged = Path(first.staged_path(str(tmp_path / "A.topos")))
        staged.write_bytes(b"CAPTURED FRAME")
        first.add(str(tmp_path / "A.topos"))

        second = StillCaptureTransaction(str(tmp_path))
        assert staged.exists()          # 第一笔的 staging 完好
        second.rollback()
        assert staged.exists()          # 第二笔回滚也不碰第一笔
        first.rollback()
        assert not staged.exists()
        assert not (tmp_path / "A.topos").exists()

    def test_recovery_skips_registered_txn_until_owner_gone(self, tmp_path):
        txn = StillCaptureTransaction(str(tmp_path), recover_leftovers=False)
        staged = Path(txn.staged_path(str(tmp_path / "A.topos")))
        staged.write_bytes(b"DATA")
        txn.add(str(tmp_path / "A.topos"))
        results = recover_still_transactions_detailed(str(tmp_path))
        assert [r.outcome for r in results] == ["skipped_active"]
        assert staged.exists()
        # 所有者"消亡"（注销＋释放）后：同一现场可恢复
        txn._release_locks()
        results = recover_still_transactions_detailed(str(tmp_path))
        assert [r.outcome for r in results] == ["recovered"]
        assert not staged.exists()

    def test_registry_counts_references(self, tmp_path):
        token = core.ACTIVE_TXNS.register(str(tmp_path))
        core.ACTIVE_TXNS.register(str(tmp_path))     # 同目录两笔在场
        assert core.ACTIVE_TXNS.is_active(str(tmp_path))
        core.ACTIVE_TXNS.unregister(token)
        assert core.ACTIVE_TXNS.is_active(str(tmp_path))   # 还剩一笔
        core.ACTIVE_TXNS.unregister(token)
        assert not core.ACTIVE_TXNS.is_active(str(tmp_path))
        core.ACTIVE_TXNS.unregister(token)           # 多余注销无害


class TestConstructorFailureReleases:
    def test_failed_init_releases_lock_for_next_request(self, tmp_path,
                                                        monkeypatch):
        def broken_mkdtemp(*args, **kwargs):
            raise OSError("disk full during staging mkdir")

        monkeypatch.setattr(
            "src.features.color.core.still_capture_transaction."
            "tempfile.mkdtemp", broken_mkdtemp)
        with pytest.raises(OSError):
            StillCaptureTransaction(str(tmp_path))
        monkeypatch.undo()              # 恢复磁盘可用
        # 新请求不被上一笔的失败卡死
        txn = StillCaptureTransaction(str(tmp_path))
        txn.rollback()

    def test_failed_init_leaves_no_registration(self, tmp_path, monkeypatch):
        def broken_mkdtemp(*args, **kwargs):
            raise OSError("no space")

        monkeypatch.setattr(
            "src.features.color.core.still_capture_transaction."
            "tempfile.mkdtemp", broken_mkdtemp)
        with pytest.raises(OSError):
            StillCaptureTransaction(str(tmp_path))
        leftovers = [p for p in os.listdir(tmp_path)]
        assert leftovers == []                     # 无 staging 残留


class TestLockTableHygiene:
    def test_table_releases_entries_when_unused(self, tmp_path):
        baseline = core._TABLE.size()     # 其他用例可能仍持有目录（不强求 0）
        keys = core.acquire_directories(str(tmp_path))
        assert core._TABLE.size() == baseline + 1
        core.release_directories(keys)
        assert core._TABLE.size() == baseline   # 无使用者即回收

    def test_nested_same_thread_acquire_is_allowed(self, tmp_path):
        baseline = core._TABLE.size()
        outer = core.acquire_directories(str(tmp_path))
        inner = core.acquire_directories(str(tmp_path))   # 服务内嵌套恢复
        assert core._TABLE.size() == baseline + 1         # 同键不重复建条目
        core.release_directories(inner)
        assert core._TABLE.size() == baseline + 1
        core.release_directories(outer)
        assert core._TABLE.size() == baseline


class TestCrossProcessLease:
    def test_other_process_holding_dir_blocks_new_txn(self, tmp_path):
        """两个独立进程同目标：明确 busy，不互删 staging（SR01 验收）。"""
        marker = tmp_path / "child-ready.marker"
        child = subprocess.Popen(
            [sys.executable, "-I", "-c", textwrap.dedent(f"""
                import sys, time
                sys.path.insert(0, {str(REPO_ROOT)!r})
                from src.features.color.core.still_capture_transaction \\
                    import StillCaptureTransaction
                txn = StillCaptureTransaction({str(tmp_path)!r},
                                              recover_leftovers=False)
                staged = txn.staged_path({str(tmp_path / "A.topos")!r})
                open(staged, "wb").write(b"CHILD FRAME")
                txn.add({str(tmp_path / "A.topos")!r})
                open({str(marker)!r}, "wb").write(b"ready")
                time.sleep(30)
            """)],
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 10
            while not marker.exists() and time.time() < deadline:
                if child.poll() is not None:
                    pytest.fail("child transaction process died early")
                time.sleep(0.05)
            assert marker.exists()
            with pytest.raises(StillCaptureTransactionError):
                StillCaptureTransaction(str(tmp_path))    # 他进程在执行
            assert not (tmp_path / "A.topos").exists()    # 未互删
        finally:
            child.terminate()
            child.wait(timeout=10)
        # 子进程退出（OS 释放租约）后新事务可用
        txn = StillCaptureTransaction(str(tmp_path))
        txn.rollback()


class TestFsyncContract:
    def test_real_io_error_raises(self, tmp_path, monkeypatch):
        import errno as _errno
        real_fsync = os.fsync

        def eio(fd):
            raise OSError(_errno.EIO, "I/O error")

        monkeypatch.setattr(core.os, "fsync", eio)
        with pytest.raises(OSError):
            core.fsync_dir(str(tmp_path))
        with pytest.raises(OSError):
            core.fsync_file(__file__)
        del real_fsync

    def test_unsupported_platform_errors_are_silent(self, tmp_path,
                                                    monkeypatch):
        import errno as _errno

        def einval(fd):
            raise OSError(_errno.EINVAL, "invalid argument")

        monkeypatch.setattr(core.os, "fsync", einval)
        core.fsync_dir(str(tmp_path))              # 平台不支持：静默
        core.fsync_file(__file__)
