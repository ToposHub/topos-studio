"""B2：安全文件事务核心（no-replace 原子 / 策略矩阵 / 取消 / 错误分类）。"""
from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest

from src.features.files.transactions import (
    CancelToken,
    FileTransactionService,
    OpError,
)


@pytest.fixture()
def svc():
    return FileTransactionService()


@pytest.fixture()
def workdir(tmp_path):
    source = tmp_path / "src"
    target = tmp_path / "dst"
    source.mkdir()
    target.mkdir()
    (source / "a.png").write_bytes(b"payload-a")
    (source / "b.txt").write_bytes(b"payload-b")
    return tmp_path


# ---------------------------------------------------------------- no-replace 原子


def test_move_never_replaces_existing_file(svc, workdir):
    """error 策略：目标存在 → 冲突，双方字节原样（绝不覆盖）。"""
    (workdir / "dst" / "a.png").write_bytes(b"existing")
    result = svc.move(workdir / "src" / "a.png", workdir / "dst")
    assert result.status == "conflict" and result.code == "DEST_EXISTS"
    assert (workdir / "dst" / "a.png").read_bytes() == b"existing"
    assert (workdir / "src" / "a.png").read_bytes() == b"payload-a"


def test_same_volume_move_preserves_inode_and_content(svc, workdir):
    src = workdir / "src" / "a.png"
    inode_before = src.stat().st_ino
    result = svc.move(src, workdir / "dst")
    assert result.ok
    moved = workdir / "dst" / "a.png"
    assert moved.read_bytes() == b"payload-a"
    assert moved.stat().st_ino == inode_before, "同卷 move 走 link+unlink，inode 不变"
    assert not src.exists()


def test_rename_policy_auto_uniques(svc, workdir):
    (workdir / "dst" / "a.png").write_bytes(b"first")
    result = svc.move(workdir / "src" / "a.png", workdir / "dst", policy="rename")
    assert result.ok
    assert result.destination.endswith("a 2.png")
    assert (workdir / "dst" / "a 2.png").read_bytes() == b"payload-a"
    assert (workdir / "dst" / "a.png").read_bytes() == b"first"


def test_overwrite_policy_replaces_file_content(svc, workdir):
    (workdir / "dst" / "a.png").write_bytes(b"old")
    result = svc.move(workdir / "src" / "a.png", workdir / "dst", policy="overwrite")
    assert result.ok
    assert (workdir / "dst" / "a.png").read_bytes() == b"payload-a"


def test_overwrite_policy_refuses_directories(svc, workdir):
    (workdir / "dst" / "folder").mkdir()
    (workdir / "src" / "folder").mkdir()
    (workdir / "src" / "folder" / "x").write_bytes(b"x")
    result = svc.move(workdir / "src" / "folder", workdir / "dst", policy="overwrite")
    assert result.status == "failed" and result.code == "POLICY_FORBIDDEN"
    assert (workdir / "dst" / "folder").is_dir() and not any(
        (workdir / "dst" / "folder").iterdir()
    )


def test_concurrent_same_destination_is_claimed(svc, workdir):
    claim_key = str(workdir / "dst" / "a.png")
    svc._claim(claim_key)
    try:
        result = svc.move(workdir / "src" / "a.png", workdir / "dst")
        assert result.status == "failed" and result.code == "DEST_BUSY"
    finally:
        svc._release(claim_key)


# ---------------------------------------------------------------- 软硬链接政策


def test_symlink_source_is_rejected(svc, workdir):
    real = workdir / "src" / "b.txt"
    link = workdir / "src" / "link.txt"
    link.symlink_to(real)
    result = svc.move(link, workdir / "dst")
    assert result.status == "failed" and result.code == "SOURCE_SYMLINK"
    assert link.is_symlink(), "链接本身原样保留，未解引用"


def test_symlink_destination_is_never_traversed(svc, workdir):
    victim = workdir / "dst" / "victim.png"
    victim.write_bytes(b"victim")
    link = workdir / "dst" / "a.png"
    link.symlink_to(victim)
    result = svc.move(workdir / "src" / "a.png", workdir / "dst")
    assert result.status == "conflict"
    assert victim.read_bytes() == b"victim"
    assert link.is_symlink(), "链接未被替换为实体文件"


def test_copy_skips_symlink_children_in_directories(svc, workdir):
    folder = workdir / "src" / "bundle"
    folder.mkdir()
    (folder / "real.txt").write_bytes(b"r")
    (folder / "alias.txt").symlink_to(folder / "real.txt")
    result = svc.copy(folder, workdir / "dst")
    assert result.ok
    copied = workdir / "dst" / "bundle"
    assert (copied / "real.txt").read_bytes() == b"r"
    assert not (copied / "alias.txt").exists(), "目录拷贝不复制符号链接"


# ---------------------------------------------------------------- 目录语义


def test_directory_move_conflict_and_success(svc, workdir):
    (workdir / "dst" / "bundle").mkdir()
    (workdir / "dst" / "bundle" / "occupant").write_bytes(b"o")
    bundle = workdir / "src" / "bundle"
    bundle.mkdir()
    (bundle / "item").write_bytes(b"i")

    conflict = svc.move(bundle, workdir / "dst")
    assert conflict.status == "conflict"

    result = svc.move(bundle, workdir / "dst", policy="rename")
    assert result.ok and result.destination.endswith("bundle 2")
    assert (workdir / "dst" / "bundle 2" / "item").read_bytes() == b"i"
    assert not bundle.exists()


def test_directory_rename_keeps_children(svc, workdir):
    bundle = workdir / "src"
    result = svc.rename(bundle, "src-renamed")
    assert result.ok
    assert (workdir / "src-renamed" / "a.png").read_bytes() == b"payload-a"


def test_directory_move_into_own_subtree_is_rejected(svc, workdir):
    """B7 验收项：目录重叠（目标在源子树内）必须拒绝。"""
    bundle = workdir / "src"
    (bundle / "nested").mkdir()
    result = svc.move(bundle, bundle / "nested")
    assert result.status == "failed" and result.code == "POLICY_FORBIDDEN"
    assert bundle.is_dir() and (bundle / "a.png").exists(), "源原样"
    # 移到子树自身路径（自引用）同样拒绝
    self_move = svc.move(bundle, bundle)
    assert self_move.status == "failed" and self_move.code == "POLICY_FORBIDDEN"


def test_rename_rejects_path_in_new_name(svc, workdir):
    result = svc.rename(workdir / "src" / "a.png", "../escape.png")
    assert result.status == "failed" and result.code == "BAD_NAME"
    assert (workdir / "src" / "a.png").exists()


def test_mkdir_conflict_and_create(svc, workdir):
    assert svc.mkdir(workdir / "dst" / "new").ok
    again = svc.mkdir(workdir / "dst" / "new")
    assert again.status == "conflict" and again.code == "DEST_EXISTS"
    link = workdir / "dst" / "link-dir"
    link.symlink_to(workdir / "dst" / "new")
    result = svc.mkdir(link)
    assert result.status == "conflict", "已存在的符号链接不穿过创建"


# ---------------------------------------------------------------- 跨卷（注入）


def test_cross_volume_move_via_staging_publish(svc, workdir, monkeypatch):
    from src.features.files.transactions import ops as ops_module

    monkeypatch.setattr(ops_module, "same_volume", lambda a, b: False)
    src = workdir / "src" / "a.png"
    inode_before = src.stat().st_ino
    result = svc.move(src, workdir / "dst")
    assert result.ok
    moved = workdir / "dst" / "a.png"
    assert moved.read_bytes() == b"payload-a"
    assert moved.stat().st_ino != inode_before, "跨卷必然换 inode"
    assert not src.exists()
    assert not list((workdir / "dst").glob("*.topos-tmp")), "staging 已清理"
    assert not list((workdir / "dst").glob(".a.png.*")), "无隐藏残留"


def test_cross_volume_conflict_leaves_no_trace(svc, workdir, monkeypatch):
    from src.features.files.transactions import ops as ops_module

    monkeypatch.setattr(ops_module, "same_volume", lambda a, b: False)
    (workdir / "dst" / "a.png").write_bytes(b"occupied")
    result = svc.move(workdir / "src" / "a.png", workdir / "dst")
    assert result.status == "conflict" and result.code == "DEST_EXISTS"
    assert (workdir / "dst" / "a.png").read_bytes() == b"occupied"
    assert not list((workdir / "dst").glob(".*")), "无 staging 残留"
    assert (workdir / "src" / "a.png").exists()


def test_cross_volume_copy_preserves_mode_and_mtime(svc, workdir, monkeypatch):
    from src.features.files.transactions import ops as ops_module

    monkeypatch.setattr(ops_module, "same_volume", lambda a, b: False)
    src = workdir / "src" / "b.txt"
    os.chmod(src, 0o640)
    os.utime(src, (1000000, 1000000))
    result = svc.copy(src, workdir / "dst")
    assert result.ok
    copied = workdir / "dst" / "b.txt"
    assert stat.S_IMODE(copied.stat().st_mode) == 0o640
    assert int(copied.stat().st_mtime) == 1000000


# ---------------------------------------------------------------- 取消


def test_cancel_mid_copy_leaves_no_destination(svc, workdir, monkeypatch):
    from src.features.files.transactions import ops as ops_module

    monkeypatch.setattr(ops_module, "same_volume", lambda a, b: False)
    monkeypatch.setattr(ops_module, "CHUNK_SIZE", 4)  # 9 字节负载分 3 块

    class _CancelAfter(CancelToken):
        """第 N 次检查后请求取消：取消落在拷贝块边界而非条目边界。"""

        def __init__(self, after: int) -> None:
            super().__init__()
            self._checks = 0
            self._after = after

        @property
        def requested(self) -> bool:
            self._checks += 1
            return self._checks > self._after

    cancel = _CancelAfter(after=1)  # 首块通过，第二块前取消
    result = svc.move(workdir / "src" / "a.png", workdir / "dst", cancel=cancel)
    assert result.status == "cancelled" and result.code == "CANCELLED"
    assert not (workdir / "dst" / "a.png").exists(), "取消不留目标"
    assert not list((workdir / "dst").glob(".*")), "取消清理 staging"
    assert (workdir / "src" / "a.png").read_bytes() == b"payload-a", "源完好"


# ---------------------------------------------------------------- 权限/磁盘满分类


def test_permission_error_maps_to_code(svc, workdir, monkeypatch):
    locked = workdir / "locked"
    locked.mkdir()
    (locked / "secret.txt").write_bytes(b"s")
    os.chmod(locked, 0o500)
    try:
        if os.access(locked, os.W_OK):
            pytest.skip("运行环境无法模拟只读目录（root）")
        result = svc.move(locked / "secret.txt", workdir / "dst")
        assert result.status == "failed" and result.code == "PERMISSION_DENIED"
        assert result.code not in ("IO_ERROR",)
    finally:
        os.chmod(locked, 0o700)


def test_disk_full_maps_to_code(svc, workdir, monkeypatch):
    from src.features.files.transactions import ops as ops_module

    monkeypatch.setattr(ops_module, "same_volume", lambda a, b: False)

    def _explode(*args, **kwargs):
        import errno

        raise OSError(errno.ENOSPC, "mock disk full")

    monkeypatch.setattr("os.fsync", _explode)
    result = svc.move(workdir / "src" / "a.png", workdir / "dst")
    assert result.status == "failed" and result.code == "DISK_FULL"
    assert result.code == "DISK_FULL"
    assert not (workdir / "dst" / "a.png").exists()
    assert not list((workdir / "dst").glob(".*")), "失败清理 staging"
    assert (workdir / "src" / "a.png").exists()


def test_missing_source_and_parent_classified(svc, workdir):
    missing = svc.move(workdir / "src" / "ghost.png", workdir / "dst")
    assert missing.status == "failed" and missing.code == "SOURCE_MISSING"
    orphan = svc.move(workdir / "src" / "a.png", workdir / "no-such-dir")
    assert orphan.status == "failed" and orphan.code == "DEST_PARENT_MISSING"


# ---------------------------------------------------------------- 身份回调


def test_identity_moved_callback_fires_for_move_not_copy(svc, workdir):
    seen: list[tuple[str, str]] = []
    svc.move(workdir / "src" / "a.png", workdir / "dst",
             on_identity_moved=lambda old, new: seen.append((old, new)))
    svc.copy(workdir / "src" / "b.txt", workdir / "dst")  # copy 无身份迁移回调
    assert len(seen) == 1
    old, new = seen[0]
    assert Path(old).name == "a.png" and Path(new).parent.name == "dst"
