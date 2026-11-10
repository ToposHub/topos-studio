"""BR01：统一文件事务的故障保全与恢复验收。

对应《Gallery/Viewport 后端复查整改计划》BR01（2026-11-10）：
- 意图先行：物理 replace 之前 journal 已登记路径与内容身份；
- 恢复按"journal 意图 × 磁盘实际状态 × 内容身份"收敛，不信任滞后的
  installed/backed_up 标志；
- 保全优先：恢复失败/状态矛盾/坏 JSON/未知 schema/库外路径一律保留
  事务目录与唯一副本（pending/conflict/quarantined），全部后置条件
  验证通过才清理；
- copy-move 独立恢复分支；trash 恢复幂等；目录互斥防并发互踩。
"""
from __future__ import annotations

import json
import os
import threading
import unittest.mock
from pathlib import Path

import pytest

from src.features.color.core import gallery_bundle_service as bundles
from src.features.color.core import still_capture_transaction as captures
from src.features.color.core.gallery_txn_core import (
    DirectoryBusyError,
    acquire_directories,
    release_directories,
)


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    for section in ("User", "System"):
        (tmp_path / section).mkdir()
    return tmp_path


def _seed(folder: Path, stem: str, tag: str = "OLD") -> None:
    for suffix in (".topos", ".thumb.jpg", ".meta.json"):
        (folder / f"{stem}{suffix}").write_bytes(
            f"{tag}-{stem}{suffix}".encode())


# ── move 事务：崩溃窗口与保全 ─────────────────────────────────────────

class TestMoveRecovery:
    def test_crash_between_replace_and_journal_restores_old_set(
            self, root: Path):
        """安装后、journal 更新前退出：恢复把旧名原样放回（逆安装）。"""
        user = root / "User"
        _seed(user, "Old")
        service = bundles.GalleryBundleService(str(root))
        real_write = bundles._MoveTransaction._write

        def die_after_install(self):
            if any(entry.get("installed") for entry in self.entries):
                raise KeyboardInterrupt("process death before journal update")
            return real_write(self)

        with pytest.raises(KeyboardInterrupt):
            with unittest.mock.patch.object(
                    bundles._MoveTransaction, "_write", die_after_install):
                service.rename_bundle(str(user), "Old", "New")
        assert not (user / "Old.topos").exists()   # 崩溃现场：已物理搬走
        assert (user / "New.topos").exists()

        results = service.recover_gallery_transactions_detailed(str(user))
        assert [r.outcome for r in results] == ["recovered"]
        assert (user / "Old.topos").read_bytes() == b"OLD-Old.topos"
        assert not (user / "New.topos").exists()
        assert not list(user.glob(bundles.TXN_DIR_PREFIX + "*"))

    def test_failed_recovery_preserves_sole_copy_and_journal(
            self, root: Path, monkeypatch):
        """恢复时设备不可用：staging 唯一副本与 journal 都保全（pending）。"""
        user = root / "User"
        txn = user / (bundles.TXN_DIR_PREFIX + "fault")
        txn.mkdir()
        (txn / "Old.topos").write_bytes(b"ONLY COPY")
        (txn / bundles.MANIFEST_NAME).write_text(json.dumps({
            "state": bundles.STATE_INSTALLING,
            "entries": [{"src": str(user / "Old.topos"),
                         "staged": str(txn / "Old.topos"),
                         "dst": str(user / "New.topos"),
                         "installed": False}]}))
        service = bundles.GalleryBundleService(str(root))
        real_replace = os.replace

        def unavailable(src, dst):
            raise OSError("restore device unavailable")

        monkeypatch.setattr(
            "src.features.color.core.gallery_bundle_service.os.replace",
            unavailable)
        results = service.recover_gallery_transactions_detailed(str(user))
        monkeypatch.setattr(os, "replace", real_replace)
        assert [r.outcome for r in results] == ["pending"]
        assert (txn / "Old.topos").read_bytes() == b"ONLY COPY"
        assert (txn / bundles.MANIFEST_NAME).exists()
        # 设备恢复后重试收敛
        assert service.recover_gallery_transactions(str(user))
        assert (user / "Old.topos").read_bytes() == b"ONLY COPY"

    def test_foreign_pid_txn_left_alone(self, root: Path):
        """journal 归属其他存活进程（本进程 pid 除外）：不清扫。"""
        import socket
        foreign_pid = os.getpid() ^ 1 if (os.getpid() ^ 1) > 1 else 2
        # 用一个确定存活的 pid：本测试进程自己派生不了——用 os.getpid()+1
        # 不可靠，改由 monkeypatch 不行（需要真实活进程）——取 pid 1
        # （launchd，恒存活）。
        foreign_pid = 1
        del foreign_pid, socket
        user = root / "User"
        txn = user / (bundles.TXN_DIR_PREFIX + "pid")
        txn.mkdir()
        (txn / bundles.MANIFEST_NAME).write_text(json.dumps({
            "pid": 1, "state": bundles.STATE_INSTALLING, "entries": []}))
        service = bundles.GalleryBundleService(str(root))
        results = service.recover_gallery_transactions_detailed(str(user))
        assert [r.outcome for r in results] == ["skipped_active"]
        assert txn.exists()          # 不动其他进程的在途事务

    def test_bad_journal_quarantined_not_deleted(self, root: Path):
        user = root / "User"
        txn = user / (bundles.TXN_DIR_PREFIX + "bad")
        txn.mkdir()
        (txn / bundles.MANIFEST_NAME).write_text("{ not json")
        (txn / "Old.topos").write_bytes(b"KEEP")
        service = bundles.GalleryBundleService(str(root))
        results = service.recover_gallery_transactions_detailed(str(user))
        assert [r.outcome for r in results] == ["quarantined"]
        assert (txn / "Old.topos").exists()    # 坏日志：保全不动
        assert (txn / bundles.MANIFEST_NAME).exists()

    def test_unknown_schema_quarantined(self, root: Path):
        user = root / "User"
        txn = user / (bundles.TXN_DIR_PREFIX + "future")
        txn.mkdir()
        (txn / bundles.MANIFEST_NAME).write_text(json.dumps({
            "schema": 99, "state": bundles.STATE_INSTALLING, "entries": []}))
        service = bundles.GalleryBundleService(str(root))
        results = service.recover_gallery_transactions_detailed(str(user))
        assert [r.outcome for r in results] == ["quarantined"]
        assert txn.exists()

    def test_outside_root_journal_quarantined(self, root: Path, tmp_path):
        user = root / "User"
        txn = user / (bundles.TXN_DIR_PREFIX + "evil")
        txn.mkdir()
        (txn / bundles.MANIFEST_NAME).write_text(json.dumps({
            "state": bundles.STATE_INSTALLING,
            "entries": [{"src": "/etc/passwd", "staged": str(txn / "x"),
                         "dst": str(user / "x"), "installed": False}]}))
        service = bundles.GalleryBundleService(str(root))
        results = service.recover_gallery_transactions_detailed(str(user))
        assert [r.outcome for r in results] == ["quarantined"]
        assert os.path.exists("/etc/passwd")    # 库外文件未被触碰
        assert txn.exists()

    def test_rolled_back_leftover_cleaned(self, root: Path):
        user = root / "User"
        txn = user / (bundles.TXN_DIR_PREFIX + "done")
        txn.mkdir()
        (txn / bundles.MANIFEST_NAME).write_text(json.dumps({
            "state": bundles.STATE_ROLLED_BACK, "entries": []}))
        service = bundles.GalleryBundleService(str(root))
        results = service.recover_gallery_transactions_detailed(str(user))
        assert [r.outcome for r in results] == ["recovered"]
        assert not txn.exists()


# ── copy-move：独立恢复分支 ────────────────────────────────────────────

class TestCopyMoveRecovery:
    @staticmethod
    def _sha(data: bytes) -> str:
        import hashlib
        return hashlib.sha256(data).hexdigest()

    def _copy_txn(self, root: Path, state: str, *,
                  with_evidence: bool = True) -> Path:
        user, target = root / "User", root / "System"
        txn = target / (bundles.TXN_DIR_PREFIX + "copy")
        txn.mkdir()
        (user / "Big.topos").write_bytes(b"SOURCE")
        (target / "Big.topos").write_bytes(b"SOURCE")     # 已安装副本
        entry = {
            "src": str(user / "Big.topos"),
            "staged": str(txn / "copy-Big.topos"),
            "dst": str(target / "Big.topos"),
            "installed": state == bundles.STATE_COMMITTED,
            "size": len(b"SOURCE")}
        if with_evidence:   # SR02 起生产格式：源/副本内容证据
            entry.update({"sha256": self._sha(b"SOURCE"),
                          "src_sha256": self._sha(b"SOURCE")})
        (txn / bundles.MANIFEST_NAME).write_text(json.dumps({
            "kind": bundles.KIND_COPY_MOVE, "state": state,
            "entries": [entry]}))
        return txn

    def test_uncommitted_copy_removes_target_copy_keeps_source(
            self, root: Path):
        txn = self._copy_txn(root, bundles.STATE_INSTALLING)
        service = bundles.GalleryBundleService(str(root))
        results = service.recover_gallery_transactions_detailed(
            str(root / "System"))
        assert [r.outcome for r in results] == ["recovered"]
        assert (root / "User" / "Big.topos").read_bytes() == b"SOURCE"
        assert not (root / "System" / "Big.topos").exists()
        assert not txn.exists()

    def test_committed_copy_continues_source_cleanup(self, root: Path):
        """committed 后中断在清源途中：按日志继续清源（源冗余）。"""
        txn = self._copy_txn(root, bundles.STATE_COMMITTED)
        service = bundles.GalleryBundleService(str(root))
        results = service.recover_gallery_transactions_detailed(
            str(root / "System"))
        assert [r.outcome for r in results] == ["recovered"]
        assert not (root / "User" / "Big.topos").exists()
        assert (root / "System" / "Big.topos").read_bytes() == b"SOURCE"
        assert not txn.exists()

    def test_foreign_file_at_copy_target_is_conflict(self, root: Path):
        txn = self._copy_txn(root, bundles.STATE_INSTALLING)
        (root / "System" / "Big.topos").write_bytes(b"FOREIGN FILE!!")
        service = bundles.GalleryBundleService(str(root))
        results = service.recover_gallery_transactions_detailed(
            str(root / "System"))
        assert [r.outcome for r in results] == ["conflict"]
        assert (root / "System" / "Big.topos").read_bytes() == \
            b"FOREIGN FILE!!"    # 内容不符：不删事务之外的新文件
        assert (root / "User" / "Big.topos").exists()

    def test_legacy_copy_without_evidence_is_preserved(self, root: Path):
        """SR02：无内容证据的旧格式日志不猜归属——副本与日志都保全。"""
        txn = self._copy_txn(root, bundles.STATE_INSTALLING,
                             with_evidence=False)
        service = bundles.GalleryBundleService(str(root))
        results = service.recover_gallery_transactions_detailed(
            str(root / "System"))
        assert [r.outcome for r in results] == ["conflict"]
        assert (root / "System" / "Big.topos").exists()
        assert (root / "User" / "Big.topos").exists()
        assert txn.exists()

    def test_committed_copy_target_corrupt_is_conflict(self, root: Path):
        """committed 清源前发现目标内容损坏：源不删，现场保全。"""
        txn = self._copy_txn(root, bundles.STATE_COMMITTED)
        (root / "System" / "Big.topos").write_bytes(b"CORRUPTED!!")
        service = bundles.GalleryBundleService(str(root))
        results = service.recover_gallery_transactions_detailed(
            str(root / "System"))
        assert [r.outcome for r in results] == ["conflict"]
        assert (root / "User" / "Big.topos").exists()   # 源保住
        assert txn.exists()

    def test_committed_copy_source_rebuilt_is_conflict(self, root: Path):
        """committed 清源前发现源被重建（内容变化）：不删用户数据。"""
        txn = self._copy_txn(root, bundles.STATE_COMMITTED)
        (root / "User" / "Big.topos").write_bytes(b"USER REBUILT")
        service = bundles.GalleryBundleService(str(root))
        results = service.recover_gallery_transactions_detailed(
            str(root / "System"))
        assert [r.outcome for r in results] == ["conflict"]
        assert (root / "User" / "Big.topos").read_bytes() == b"USER REBUILT"
        assert (root / "System" / "Big.topos").exists()
        assert txn.exists()


# ── still capture：备份窗口与回滚保全 ─────────────────────────────────

class TestStillCaptureRecovery:
    def _overwrite_txn(self, tmp_path: Path) -> captures.StillCaptureTransaction:
        target = tmp_path / "Old.topos"
        target.write_bytes(b"OLD GRADE")
        txn = captures.StillCaptureTransaction(
            str(tmp_path), recover_leftovers=False)
        Path(txn.staged_path(str(target))).write_bytes(b"NEW GRADE")
        txn.add(str(target))
        return txn

    def test_crash_after_backup_restore_old_bytes(self, tmp_path, monkeypatch):
        """备份移走后、journal 更新前退出：恢复按登记的备份路径放回。"""
        import unittest.mock
        txn = self._overwrite_txn(tmp_path)
        real_write = captures.StillCaptureTransaction._write_manifest

        def die_after_backup(self):
            if any(entry.get("backed_up") for entry in self._entries):
                raise KeyboardInterrupt("death before backup journal update")
            return real_write(self)

        with unittest.mock.patch.object(
                captures.StillCaptureTransaction, "_write_manifest",
                die_after_backup):
            with pytest.raises(KeyboardInterrupt):
                txn.commit()
        assert not (tmp_path / "Old.topos").exists()    # 崩溃现场

        results = captures.recover_still_transactions_detailed(str(tmp_path))
        assert [r.outcome for r in results] == ["recovered"]
        assert (tmp_path / "Old.topos").read_bytes() == b"OLD GRADE"

    def test_failed_rollback_preserves_backup_and_txn_dir(
            self, tmp_path, monkeypatch):
        txn = self._overwrite_txn(tmp_path)
        txn._commit_backing()

        def unavailable(src, dst):
            raise OSError("restore device unavailable")

        monkeypatch.setattr(
            "src.features.color.core.still_capture_transaction.os.replace",
            unavailable)
        with pytest.raises(OSError):
            txn.rollback()
        monkeypatch.undo()
        backup = Path(txn.backup_dir, "Old.topos")
        assert backup.read_bytes() == b"OLD GRADE"   # 唯一旧副本保全
        assert Path(txn.txn_dir).exists()
        # 设备恢复后重试收敛
        captures.recover_still_transactions(str(tmp_path))
        assert (tmp_path / "Old.topos").read_bytes() == b"OLD GRADE"

    def test_committed_with_missing_final_is_conflict(self, tmp_path):
        txn = self._overwrite_txn(tmp_path)
        txn.commit()
        (tmp_path / "Old.topos").unlink()             # 新集合缺件
        # 手工留下 committed 事务目录
        recovered = txn.txn_dir
        assert not os.path.isdir(recovered)           # commit 已清理
        txn2 = self._overwrite_txn(tmp_path)
        # 模拟 committed 遗漏：状态改写后目标被删
        txn2._state = captures.STATE_COMMITTED
        txn2._write_manifest()
        (tmp_path / "Old.topos").unlink()
        # SR01：模拟所有者进程消亡（登记注销）——在场事务是 skipped_active
        txn2._release_locks()
        results = captures.recover_still_transactions_detailed(str(tmp_path))
        assert results[0].outcome == "conflict"       # 保全，不误清备份
        assert Path(txn2.txn_dir).exists()

    def test_busy_target_rejected_not_raced(self, tmp_path):
        readied, release = threading.Event(), threading.Event()

        def hold():                                   # 另一线程的并发抓帧
            keys = acquire_directories(str(tmp_path))
            readied.set()
            release.wait(5)
            release_directories(keys)

        worker = threading.Thread(target=hold)
        worker.start()
        assert readied.wait(5)
        try:
            with pytest.raises(captures.StillCaptureTransactionError):
                self._overwrite_txn(tmp_path)         # 忙：明确失败
            # 恢复跳过在场事务（不扫它的 staging）
            assert captures.recover_still_transactions(str(tmp_path)) == []
        finally:
            release.set()
            worker.join(5)

    def test_new_files_installed_without_backup_removed_on_rollback(
            self, tmp_path):
        target = tmp_path / "Fresh.topos"             # 新建（无旧文件）
        txn = captures.StillCaptureTransaction(
            str(tmp_path), recover_leftovers=False)
        Path(txn.staged_path(str(target))).write_bytes(b"NEW")
        txn.add(str(target))
        txn._commit_backing()
        txn._commit_installing()
        txn.rollback()
        assert not target.exists()                    # 零新孤儿文件
        assert not list(tmp_path.glob(captures.TXN_DIR_PREFIX + "*"))

    def test_unexpected_target_file_left_alone(self, tmp_path):
        """staged 还在而 final 已被外部创建：冲突保全，不删别人的文件。"""
        target = tmp_path / "Fresh.topos"
        target.write_bytes(b"EXTERNAL")               # 本事务未安装
        txn = captures.StillCaptureTransaction(
            str(tmp_path), recover_leftovers=False)
        Path(txn.staged_path(str(target))).write_bytes(b"NEW")
        entry = {"final": str(target),
                 "staged": txn.staged_path(str(target)),
                 "existed": False, "old_identity": None}
        with pytest.raises(RuntimeError):
            captures._restore_entry_to_old_set(entry)
        assert target.read_bytes() == b"EXTERNAL"


# ── 目录互斥 ───────────────────────────────────────────────────────────

class TestDirectoryMutex:
    @staticmethod
    def _locked_from_other_thread(directory: Path):
        """跨线程持锁（同线程 RLock 可重入，测不出互斥）。"""
        import contextlib

        @contextlib.contextmanager
        def _hold():
            readied, release = threading.Event(), threading.Event()

            def hold():
                keys = acquire_directories(str(directory))
                readied.set()
                release.wait(5)
                release_directories(keys)

            worker = threading.Thread(target=hold)
            worker.start()
            assert readied.wait(5)
            try:
                yield
            finally:
                release.set()
                worker.join(5)

        return _hold()

    def test_concurrent_move_rejected_album_busy(self, root: Path):
        user = root / "User"
        _seed(user, "A")
        service = bundles.GalleryBundleService(str(root))
        with self._locked_from_other_thread(user):
            result = service.rename_bundle(str(user), "A", "B")
            assert result.status == "rejected"
            assert result.reason == bundles.REASON_ALBUM_BUSY
        assert service.rename_bundle(str(user), "A", "B").succeeded

    def test_recovery_skips_locked_folder(self, root: Path):
        user = root / "User"
        txn = user / (bundles.TXN_DIR_PREFIX + "held")
        txn.mkdir()
        (txn / bundles.MANIFEST_NAME).write_text(json.dumps({
            "state": bundles.STATE_ROLLED_BACK, "entries": []}))
        service = bundles.GalleryBundleService(str(root))
        with self._locked_from_other_thread(user):
            assert service.recover_gallery_transactions(str(user)) == []
            assert txn.exists()                       # 活跃事务在场：不扫
        assert service.recover_gallery_transactions(str(user))
        assert not txn.exists()

    def test_two_folder_move_releases_both(self, root: Path):
        user, system = root / "User", root / "System"
        _seed(user, "M")
        service = bundles.GalleryBundleService(str(root))
        assert service.move_bundle(str(user), "M", str(system)).succeeded
        # 锁已全释放：后续操作可正常进入
        assert service.move_bundle(str(system), "M", str(user)).succeeded


# ── trash 恢复幂等 ─────────────────────────────────────────────────────

class TestTrashRestoreIdempotent:
    def _trash_bundle(self, root: Path) -> None:
        user = root / "User"
        _seed(user, "Doomed")
        service = bundles.GalleryBundleService(str(root))
        assert service.delete_bundle(str(user), "Doomed").succeeded
        assert not (user / "Doomed.topos").exists()

    def test_partial_restore_retry_succeeds(self, root: Path, monkeypatch):
        self._trash_bundle(root)
        user = root / "User"
        service = bundles.GalleryBundleService(str(root))
        trash_ids = [r["trash_id"] for r in service.list_trash()]
        assert len(trash_ids) == 1
        # 第一个成员搬回后模拟失败（半恢复现场）
        real_replace = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_replace(src, dst)
            raise OSError("device left")

        monkeypatch.setattr(
            "src.features.color.core.gallery_bundle_service.os.replace",
            flaky)
        first = service.restore_from_trash(trash_ids[0])
        monkeypatch.setattr(os, "replace", real_replace)
        assert first.status in ("rejected", "partial")
        moved = [p.name for p in user.iterdir()
                 if p.name.startswith("Doomed")]
        assert moved                            # 部分成员已回原位
        # 重试：已在原位的成员不再挡路，全量收敛
        second = service.restore_from_trash(trash_ids[0])
        assert second.succeeded, second
        for suffix in (".topos", ".thumb.jpg", ".meta.json"):
            assert (user / f"Doomed{suffix}").exists()
        assert not service.list_trash()

    def test_conflict_keeps_trash_copy(self, root: Path):
        self._trash_bundle(root)
        user = root / "User"
        (user / "Doomed.topos").write_bytes(b"NEW LOCAL")   # 目标被占
        service = bundles.GalleryBundleService(str(root))
        trash_ids = [r["trash_id"] for r in service.list_trash()]
        result = service.restore_from_trash(trash_ids[0])
        assert result.status == "partial"
        assert (user / "Doomed.topos").read_bytes() == b"NEW LOCAL"
        assert len(service.list_trash()) == 1    # trash 副本保留可重试
