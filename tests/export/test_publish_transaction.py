"""P5-T6：publish_transaction 专测。

M21.1b 成组发布状态机的公共 API 覆盖：
- publish_file_group（成功 / 空组 / 缺 temp 回滚 / 备份复位）
- cleanup_group_backups

全部文件操作落在 tmp_path；回滚路径验证"旧成功产物逐字节保留"。
"""

from __future__ import annotations

import os

import pytest

from src.shared.export.publish_transaction import (
    PublishFile,
    PublishResult,
    cleanup_group_backups,
    publish_file_group,
)


def _write(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _group(tmp_path, name: str) -> PublishFile:
    return PublishFile(
        temp_path=str(tmp_path / f"{name}.tmp"),
        final_path=str(tmp_path / f"{name}.final"),
        kind="media",
    )


class TestSuccessfulPublish:
    def test_publish_moves_temps_to_finals(self, tmp_path) -> None:
        files = [_group(tmp_path, "a"), _group(tmp_path, "b")]
        for f in files:
            _write(f.temp_path, f"new-{f.final_path}")

        result = publish_file_group(files)

        assert isinstance(result, PublishResult)
        assert result.ok is True
        assert result.error == ""
        assert result.published == (files[0].final_path, files[1].final_path)
        for f in files:
            assert _read(f.final_path) == f"new-{f.final_path}"
            assert not os.path.exists(f.temp_path)

    def test_publish_backs_up_existing_finals(self, tmp_path) -> None:
        f = _group(tmp_path, "a")
        _write(f.final_path, "old-content")
        _write(f.temp_path, "new-content")

        result = publish_file_group([f])

        assert result.ok is True
        assert _read(f.final_path) == "new-content"
        assert _read(f.final_path + ".topos-old") == "old-content"

    def test_manifest_kind_accepted(self, tmp_path) -> None:
        media = _group(tmp_path, "m")
        manifest = PublishFile(temp_path=str(tmp_path / "mf.tmp"),
                               final_path=str(tmp_path / "mf.final"),
                               kind="manifest")
        for f in (media, manifest):
            _write(f.temp_path, "x")
        result = publish_file_group([media, manifest])
        assert result.ok is True
        assert result.published[-1] == manifest.final_path


class TestFailurePaths:
    def test_empty_group_rejected(self) -> None:
        result = publish_file_group([])
        assert result.ok is False
        assert result.stage == "backup"
        assert result.error  # 非空错误说明

    def test_missing_temp_rolls_back_moved_file_and_backups(
            self, tmp_path) -> None:
        """move 阶段失败：已就位的新文件退回 temp，旧备份复位。"""
        f1 = _group(tmp_path, "a")
        f2 = _group(tmp_path, "b")
        _write(f1.final_path, "old-a")
        _write(f2.final_path, "old-b")
        _write(f1.temp_path, "new-a")
        # f2 的 temp 缺失

        result = publish_file_group([f1, f2])

        assert result.ok is False
        assert result.stage == "move"
        assert "b.tmp" in result.error
        # 回滚后：新文件退回 temp
        assert _read(f1.temp_path) == "new-a"
        # 旧产物逐字节保留
        assert _read(f1.final_path) == "old-a"
        assert _read(f2.final_path) == "old-b"
        # 备份被复位消费掉
        assert not os.path.exists(f1.final_path + ".topos-old")

    def test_missing_temp_before_any_move_reports_backup_stage(
            self, tmp_path) -> None:
        f1 = _group(tmp_path, "a")
        _write(f1.final_path, "old-a")
        # temp 缺失，未发生任何 move

        result = publish_file_group([f1])

        assert result.ok is False
        assert result.stage == "backup"
        assert _read(f1.final_path) == "old-a"

    def test_missing_temp_without_existing_final(self, tmp_path) -> None:
        f1 = _group(tmp_path, "a")
        _write(f1.temp_path, "x")
        f2 = _group(tmp_path, "b")
        # 两个 final 均不存在，f2 temp 缺失

        result = publish_file_group([f1, f2])

        assert result.ok is False
        # f1 已 move → 回滚退回 temp
        assert _read(f1.temp_path) == "x"
        assert not os.path.exists(f1.final_path)


class TestCleanupBackups:
    def test_cleanup_removes_backups_and_counts(self, tmp_path) -> None:
        f = _group(tmp_path, "a")
        _write(f.final_path, "old")
        _write(f.temp_path, "new")
        assert publish_file_group([f]).ok is True
        assert os.path.exists(f.final_path + ".topos-old")

        removed = cleanup_group_backups([f])

        assert removed == 1
        assert not os.path.exists(f.final_path + ".topos-old")

    def test_cleanup_without_backups_returns_zero(self, tmp_path) -> None:
        f = _group(tmp_path, "a")
        assert cleanup_group_backups([f]) == 0

    def test_cleanup_only_touches_own_group_suffix(self, tmp_path) -> None:
        f = _group(tmp_path, "a")
        _write(f.final_path, "old")
        _write(f.final_path + ".topos-old", "backup")
        _write(f.final_path + ".other", "unrelated")

        removed = cleanup_group_backups([f], backup_suffix=".other")

        assert removed == 1
        assert not os.path.exists(f.final_path + ".other")
        assert os.path.exists(f.final_path + ".topos-old")
