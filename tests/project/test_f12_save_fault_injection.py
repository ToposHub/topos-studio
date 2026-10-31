"""F1.2 · 工程保存故障路径注入——保存中断/半写/损坏候选/备份恢复。

隔离约束：全部注入发生在 tmp_path 的临时工程与 monkeypatch 层，不触碰
用户工程、不在用户盘制造磁盘满（以 os.replace 抛错模拟"无法晋升"）。

契约（F1.2 验收；save 失败返回 False、load 失败返回 (None, None)，不抛）：
  1. 保存中断/写入失败 → 最后可用工程字节不变（原子晋升保证）
  2. 损坏候选（截断归档）→ 拒绝加载且不影响最后可用工程
  3. 备份在主文件损坏后仍可用，复制回主路径即恢复（恢复入口）
"""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.edit_core

import src.shared.project.topos_project_io as io_mod
from src.shared.project.topos_project import ToposProject
from src.shared.project.topos_project_io import ToposProjectIO, ToposProjectIOConfig


def _project(name: str = "F1.2Fault") -> ToposProject:
    return ToposProject.create_single_asset(
        name=name, asset_path="/tmp/f12_asset.jpg", media_type="image")


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_ok(path: Path) -> bool:
    project, payload = ToposProjectIO.load(str(path), ToposProjectIOConfig())
    return project is not None


class TestSaveInterruption:
    def test_replace_failure_leaves_last_good_intact(self, tmp_path: Path, monkeypatch) -> None:
        target = tmp_path / "proj.topos"
        assert ToposProjectIO.save(_project("v1"), str(target), ToposProjectIOConfig())
        good_hash = _hash(target)

        def _boom(*args, **kwargs):
            raise OSError("simulated promote failure (disk full guard)")

        monkeypatch.setattr(io_mod.os, "replace", _boom)
        result = ToposProjectIO.save(_project("v2"), str(target), ToposProjectIOConfig())
        monkeypatch.undo()
        assert result is False, "晋升失败应优雅返回 False"
        assert _hash(target) == good_hash, "保存中断后最后可用工程必须字节不变"
        # 恢复后重存成功（故障不留下无法再保存的状态）
        assert ToposProjectIO.save(_project("v2"), str(target), ToposProjectIOConfig())
        assert _hash(target) != good_hash
        assert _load_ok(target)

    def test_partial_zip_write_failure_never_leaves_unloadable_target(
            self, tmp_path: Path, monkeypatch) -> None:
        """归档中途写失败：save 或整体失败（False、目标不变），或按设计
        容忍单模块缺失（True、降级保存）——两种结果目标都必须保持可加载，
        绝不留下半写的不可解析归档。"""
        target = tmp_path / "proj.topos"
        assert ToposProjectIO.save(_project("v1"), str(target), ToposProjectIOConfig())
        good_hash = _hash(target)

        original_write = zipfile.ZipFile.write

        def _failing_write(self, *args, **kwargs):
            original_write(self, *args, **kwargs)
            raise OSError("simulated mid-archive write failure")

        monkeypatch.setattr(zipfile.ZipFile, "write", _failing_write)
        result = ToposProjectIO.save(_project("v2"), str(target), ToposProjectIOConfig())
        monkeypatch.undo()
        if result is False:
            assert _hash(target) == good_hash, "整体失败时最后可用工程必须字节不变"
        assert _load_ok(target), "目标必须保持可加载（不允许半写不可解析归档）"


class TestCorruptCandidate:
    def _make_good(self, tmp_path: Path) -> Path:
        target = tmp_path / "proj.topos"
        assert ToposProjectIO.save(_project("good"), str(target), ToposProjectIOConfig())
        return target

    def test_truncated_archive_fails_cleanly(self, tmp_path: Path) -> None:
        good = self._make_good(tmp_path)
        truncated = tmp_path / "truncated.topos"
        truncated.write_bytes(good.read_bytes()[: max(1, good.stat().st_size // 2)])
        project, payload = ToposProjectIO.load(str(truncated), ToposProjectIOConfig())
        assert project is None, "截断归档必须优雅拒绝，不得返回半成品工程"

    def test_truncated_candidate_does_not_affect_last_good(self, tmp_path: Path) -> None:
        good = self._make_good(tmp_path)
        good_hash = _hash(good)
        candidate = tmp_path / "candidate.topos"
        candidate.write_bytes(good.read_bytes()[:10])
        project, _payload = ToposProjectIO.load(str(candidate), ToposProjectIOConfig())
        assert project is None
        assert _hash(good) == good_hash
        assert _load_ok(good), "最后可用工程仍可正常加载"


class TestBackupRecovery:
    def test_backup_survives_main_corruption_and_restores(self, tmp_path: Path) -> None:
        from src.shared.project.save_router import SaveRouter as ProjectSaveRouter

        target = tmp_path / "proj.topos"
        assert ToposProjectIO.save(_project("v1"), str(target), ToposProjectIOConfig())
        good_hash = _hash(target)
        backup_dir = str(tmp_path / "backups")
        ok = ProjectSaveRouter._write_backup_copy(str(target), backup_dir, 3)
        assert ok
        backups = list(Path(backup_dir).glob("*.topos"))
        assert len(backups) == 1

        # 主文件被外因损坏 → 优雅拒绝
        target.write_bytes(b"corrupted-not-a-zip")
        project, _payload = ToposProjectIO.load(str(target), ToposProjectIOConfig())
        assert project is None

        # 恢复入口：备份复制回主路径 → 工程可加载且内容一致
        shutil.copyfile(backups[0], target)
        assert _hash(target) == good_hash
        assert _load_ok(target)

    def test_backup_keeps_unknown_module_payload(self, tmp_path: Path) -> None:
        """损坏-恢复往返后，最后可用工程内的未知模块 payload 保留。"""
        from src.shared.project.save_router import SaveRouter as ProjectSaveRouter

        target = tmp_path / "proj.topos"
        assert ToposProjectIO.save(_project("v1"), str(target), ToposProjectIOConfig())
        # 向归档注入"未来模块"文件（模拟旧版/未知 payload）
        with zipfile.ZipFile(str(target), "a") as zf:
            zf.writestr("future_module/v9.json", json.dumps({"x": [1, 2, 3]}))
        payload_hash = _hash(target)
        backup_dir = str(tmp_path / "backups")
        assert ProjectSaveRouter._write_backup_copy(str(target), backup_dir, 3)
        target.write_bytes(b"corrupted")
        shutil.copyfile(next(Path(backup_dir).glob("*.topos")), target)
        assert _hash(target) == payload_hash
        with zipfile.ZipFile(str(target)) as zf:
            assert zf.read("future_module/v9.json") == json.dumps({"x": [1, 2, 3]}).encode()
