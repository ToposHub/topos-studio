"""F0.3 黄金工程结构测试——结构快照、确定性、Undo 回环、序列化往返。

黄金工程由 ``tests/edit/helpers/golden_project.py`` 构建（生产命令入口 +
不可变模型），本文件冻结其结构语义，供后续功能任务作前后对比基线。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.commands import AddKeyframeCommand, CommandStack
from src.features.edit.core.timeline_v2 import TimelineSequence

from tests.edit.helpers.golden_project import (
    build_extended_golden_project,
    build_minimal_golden_project,
)
from tests.edit.helpers.golden_structure import (
    assert_structure_matches,
    snapshot_structure,
    structure_violations,
)


def _normalize_media_paths(snapshot: dict, media_root: str) -> dict:
    """两次构建的媒体目录与动态 uuid（素材/栈/嵌套 id）不同；归一化后对比。"""
    import copy
    import re

    _uuid = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

    def _walk(node):
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(v) for v in node]
        if isinstance(node, str):
            if node.startswith(f"asset:{media_root}"):
                tail = node.split("golden_media", 1)[1]
                node = f"asset:<MEDIA>{tail}"
            node = re.sub(r"\b[0-9a-f]{8}-v\d+\b", "<NESTEDTRACK>", node)
            return _uuid.sub("<UUID>", node)
        return node

    return _walk(copy.deepcopy(snapshot))


class TestMinimalGoldenProject:
    def test_structure_invariants(self) -> None:
        _project, timeline, info = build_minimal_golden_project()
        snap = snapshot_structure(timeline, _project)
        assert structure_violations(snap) == []
        assert snap["fps"] == 24.0
        assert snap["tracks"][0]["clips"][0]["clip_id"] == info["clip_id"]


class TestExtendedGoldenProject:
    def test_structure_invariants_and_coverage(self, tmp_path: Path) -> None:
        project, seqs, info = build_extended_golden_project(tmp_path)
        main = seqs["main"]
        snap = snapshot_structure(main, project)
        assert structure_violations(snap) == []

        # 覆盖断言：双顶层序列 + 子序列；链接 A/V；嵌套片段；混 fps 源；
        # 关键帧；字幕；调色栈绑定；离线素材。
        assert set(seqs) == {"main", "child", "aux"}
        clips = {c["clip_id"]: c for t in snap["tracks"] for c in t["clips"]}
        v1 = clips[info["clip_ids"]["v1"]]
        a1 = clips[info["clip_ids"]["a1"]]
        assert v1["linked_clip_id"] == a1["clip_id"]
        assert a1["linked_clip_id"] == v1["clip_id"]
        nested = clips[info["clip_ids"]["nested"]]
        assert nested["asset_ref"].startswith("asset:topos://sequence/")
        assert clips[info["clip_ids"]["adjustment"]]["effects"][0]["effect_type"] == "blur"
        assert v1["animated_param_tracks"] == ["opacity"]
        assert v1["color_stack_id"] == info["stack_id"]
        assert v1["color_shared"] is True
        sub_clips = [c for c in clips.values() if c["clip_id"].startswith("ge-sub-")]
        assert len(sub_clips) == 2
        offline = project.assets[info["asset_ids"]["offline"]]
        assert offline.is_offline is True
        mixed_clip = main.get_track_by_id("ge-v3").clips[0]
        assert (mixed_clip._source_fps_num, mixed_clip._source_fps_den) == (30000, 1001)

    def test_build_is_deterministic(self, tmp_path: Path) -> None:
        project_a, seqs_a, _ = build_extended_golden_project(tmp_path / "a")
        project_b, seqs_b, _ = build_extended_golden_project(tmp_path / "b")
        for key in ("main", "child", "aux"):
            snap_a = _normalize_media_paths(
                snapshot_structure(seqs_a[key], project_a), str(tmp_path / "a"))
            snap_b = _normalize_media_paths(
                snapshot_structure(seqs_b[key], project_b), str(tmp_path / "b"))
            ignore = ("timeline_id",) if key == "child" else ()
            assert_structure_matches(snap_a, snap_b, ignore=ignore)

    def test_command_stack_undo_roundtrip(self, tmp_path: Path) -> None:
        project, seqs, _info = build_extended_golden_project(tmp_path)
        main = seqs["main"]
        before = snapshot_structure(main, project)
        stack = CommandStack()
        after = stack.execute(
            AddKeyframeCommand.create("ge-v1", "ge-main-v1", "transform.translate_x", 24, 32.0),
            main)
        assert snapshot_structure(after, project) != before
        undone = stack.undo(after)
        assert_structure_matches(snapshot_structure(undone, project), before)

    def test_serialization_roundtrip_preserves_structure(self, tmp_path: Path) -> None:
        project, seqs, _info = build_extended_golden_project(tmp_path)
        main = seqs["main"]
        restored = TimelineSequence.from_dict(main.to_dict())
        assert_structure_matches(snapshot_structure(restored, project), snapshot_structure(main, project))
