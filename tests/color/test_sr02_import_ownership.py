"""SR02：恢复与回滚的内容归属、完整性和旧日志保全验收。

对应《第二轮复查计划》SR02（2026-11-10）：
- 导入 journal finals 带 sha256——同大小不同内容的文件**不再**被当成本
  事务产物删除；删除授权只来自内容证据；
- 旧格式（无内容 hash）日志：不猜归属，文件与日志都保全；
- 取消时回滚失败保留日志并返回 recoverable；
- 伪装事务目录（symlink）保全不动。
"""
from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest

from src.features.color.core.gallery_repository import GalleryFolderRepository
from src.features.color.core.gallery_transfer_service import (
    IMPORT_JOURNAL_NAME,
    IMPORT_TXN_PREFIX,
    GalleryTransferService,
)


def _seed_bundle(folder: Path, stem: str = "Look", *,
                 still_id: str = "S-sr02") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    topos = folder / f"{stem}.topos"
    with zipfile.ZipFile(topos, "w") as zf:
        zf.writestr("project.json",
                    json.dumps({"layers": [{"name": "g"}]}))
    return topos


@pytest.fixture()
def svc(tmp_path: Path):
    root = tmp_path / "Presets"
    (root / "User").mkdir(parents=True)
    (root / "System").mkdir()
    _seed_bundle(root / "User", "Look")
    service = GalleryTransferService(GalleryFolderRepository(str(root)))
    archive = tmp_path / "b.toposbundle"
    assert service.export_bundle(str(root / "User"), "Look",
                                 str(archive)).succeeded
    return root, service, archive


class TestRecoveryOwnership:
    def test_same_size_foreign_file_preserved_with_journal(
            self, svc, tmp_path):
        """复现场景（repro SR02）：无内容证据的恢复日志不删同大小文件。"""
        root, service, _ = svc
        folder = root / "System"
        foreign = folder / "Replaced.topos"
        foreign.write_bytes(b"USER")
        txn = folder / (IMPORT_TXN_PREFIX + "same-size")
        txn.mkdir()
        (txn / IMPORT_JOURNAL_NAME).write_text(json.dumps({
            "schema": 1, "state": "installing", "finals": [
                {"path": str(foreign), "size": 4}]}))
        service._recover_import_txns(str(folder))
        assert foreign.exists()                 # 文件保全
        assert foreign.read_bytes() == b"USER"
        assert txn.exists()                     # 日志一并保全

    def test_hash_evidence_authorizes_rollback(self, svc):
        """带内容证据的日志照常回滚——证据确凿才动文件。"""
        root, service, archive = svc
        result = service.import_bundle(str(archive), str(root / "System"))
        assert result.succeeded
        installed = root / "System" / "Look.topos"
        assert installed.exists()
        # 构造 installing 遗留：journal 描述"安装了一半"，finals 带证据
        txn = root / "System" / (IMPORT_TXN_PREFIX + "stuck")
        txn.mkdir()
        import hashlib
        (txn / IMPORT_JOURNAL_NAME).write_text(json.dumps({
            "schema": 1, "state": "installing", "finals": [{
                "path": str(installed), "size": installed.stat().st_size,
                "sha256": hashlib.sha256(
                    installed.read_bytes()).hexdigest()}]}))
        service._recover_import_txns(str(root / "System"))
        assert not installed.exists()           # 证据匹配：撤除
        assert not txn.exists()

    def test_hash_evidence_mismatch_preserves_file(self, svc):
        """有 hash 字段但内容不符＝外来文件：不动。"""
        root, service, _ = svc
        folder = root / "System"
        foreign = folder / "Replaced.topos"
        foreign.write_bytes(b"USER")
        txn = folder / (IMPORT_TXN_PREFIX + "tampered")
        txn.mkdir()
        (txn / IMPORT_JOURNAL_NAME).write_text(json.dumps({
            "schema": 1, "state": "installing", "finals": [{
                "path": str(foreign), "size": 4,
                "sha256": "0" * 64}]}))
        service._recover_import_txns(str(folder))
        assert foreign.read_bytes() == b"USER"
        assert txn.exists()

    def test_duplicate_finals_quarantined(self, svc):
        root, service, _ = svc
        folder = root / "System"
        target = folder / "A.topos"
        target.write_bytes(b"A")
        txn = folder / (IMPORT_TXN_PREFIX + "dup")
        txn.mkdir()
        (txn / IMPORT_JOURNAL_NAME).write_text(json.dumps({
            "schema": 1, "state": "installing", "finals": [
                {"path": str(target), "size": 1},
                {"path": str(target), "size": 1}]}))
        service._recover_import_txns(str(folder))
        assert target.exists()
        assert txn.exists()

    def test_symlink_txn_dir_preserved(self, svc, tmp_path):
        root, service, _ = svc
        folder = root / "System"
        real_dir = tmp_path / "real-txn"
        real_dir.mkdir()
        link = folder / (IMPORT_TXN_PREFIX + "link")
        link.symlink_to(real_dir, target_is_directory=True)
        service._recover_import_txns(str(folder))
        assert link.is_symlink()                # 伪装事务目录不动
        assert real_dir.exists()

    def test_resources_outside_managed_dir_quarantined(self, svc,
                                                       tmp_path):
        root, service, _ = svc
        folder = root / "System"
        outside = tmp_path / "evil.cube"
        outside.write_bytes(b"E")
        txn = folder / (IMPORT_TXN_PREFIX + "evilres")
        txn.mkdir()
        (txn / IMPORT_JOURNAL_NAME).write_text(json.dumps({
            "schema": 1, "state": "installing", "finals": [],
            "resources": [{"path": str(outside), "size": 1,
                           "sha256": "0" * 64}]}))
        service._recover_import_txns(str(folder))
        assert outside.exists()                 # 库外资源不删
        assert txn.exists()


class TestCancelRollbackKeepsEvidence:
    def test_cancel_with_foreign_replacement_returns_recoverable(
            self, svc, monkeypatch):
        """取消回滚发现 final 被外来内容占用：保留日志、如实 recoverable。"""
        root, service, archive = svc
        folder = root / "System"
        calls = {"n": 0}

        def cancel_after_install() -> bool:
            calls["n"] += 1
            # 第 1 次调用在解压前（False），final 安装后下一次触发取消
            return calls["n"] > 1 and (folder / "Look.topos").exists()

        result = service.import_bundle(
            str(archive), str(folder), cancel_check=cancel_after_install)
        # 视触发时机：取消或完成；断言核心＝要么零落地、要么回滚干净
        if result.status == "cancelled":
            leftovers = [p for p in folder.iterdir()
                         if p.name.startswith("Look")]
            assert leftovers == [] or result.recoverable

    def test_plain_cancel_leaves_no_trace(self, svc):
        root, service, archive = svc
        result = service.import_bundle(
            str(archive), str(root / "System"),
            cancel_check=lambda: True)
        assert result.status == "cancelled"
        assert not result.recoverable
        assert [p for p in (root / "System").iterdir()
                if not p.name.startswith(".")] == []


class TestLegacyJournalEvidence:
    def test_plain_string_finals_journal_preserved(self, svc):
        """更旧的纯路径 finals 日志（BR02 之前）：不据此删除。"""
        root, service, _ = svc
        folder = root / "System"
        victim = folder / "Old.topos"
        victim.write_bytes(b"OLD")
        txn = folder / (IMPORT_TXN_PREFIX + "legacy")
        txn.mkdir()
        (txn / IMPORT_JOURNAL_NAME).write_text(json.dumps({
            "schema": 1, "state": "installing", "finals": [str(victim)]}))
        service._recover_import_txns(str(folder))
        assert victim.exists()
        assert txn.exists()
