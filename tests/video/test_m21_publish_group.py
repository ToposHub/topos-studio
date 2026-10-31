"""M21.1b · 多文件成组发布状态机回归。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.media_core

from src.shared.export.publish_transaction import (
    PublishFile,
    cleanup_group_backups,
    publish_file_group,
)


def _stage(tmp_path: Path, old_outputs=None):
    """准备成片+sidecar+manifest 的三文件组（可带旧成功产物）。"""
    group = []
    for i, name in enumerate(("out.mp4", "out.ffmetadata", "manifest.json")):
        final = tmp_path / name
        temp = tmp_path / (name + ".topos-partial")
        temp.write_bytes(f"new-{name}".encode())
        if old_outputs:
            final.write_bytes(f"old-{name}".encode())
        group.append(PublishFile(
            temp_path=str(temp), final_path=str(final),
            kind="manifest" if i == 2 else ("media" if i == 0 else "sidecar")))
    return group


class TestPublishFileGroup:
    def test_success_publishes_all(self, tmp_path):
        group = _stage(tmp_path)
        result = publish_file_group(group)
        assert result.ok
        assert all(os.path.isfile(f.final_path) for f in group)
        assert cleanup_group_backups(group) == 0

    def test_failure_rolls_back_old_outputs(self, tmp_path):
        group = _stage(tmp_path, old_outputs=True)
        # 破坏第二个临时文件 → move 阶段失败
        os.remove(group[1].temp_path)
        result = publish_file_group(group)
        assert not result.ok
        # 旧成功产物逐字节保留
        assert (tmp_path / "out.mp4").read_bytes() == b"old-out.mp4"
        assert (tmp_path / "out.ffmetadata").read_bytes() == b"old-out.ffmetadata"
        assert (tmp_path / "manifest.json").read_bytes() == b"old-manifest.json"
        # 新文件退回 temp（第一个已 move 的回退）
        assert os.path.exists(group[0].temp_path)
        assert (Path(group[0].temp_path).read_bytes()
                == b"new-out.mp4")

    def test_old_success_cleanup_after_publish(self, tmp_path):
        group = _stage(tmp_path, old_outputs=True)
        result = publish_file_group(group)
        assert result.ok
        assert (tmp_path / "out.mp4").read_bytes() == b"new-out.mp4"
        assert cleanup_group_backups(group) == 3

    def test_empty_group_rejected(self):
        result = publish_file_group([])
        assert not result.ok and result.stage == "backup"
