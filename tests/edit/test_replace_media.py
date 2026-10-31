"""
P1-13 Replace Media 测试。

验收（计划表 P1-13）："替换素材不丢剪辑参数" —— asset_id 稳定引用使
时间线剪辑的入出点/特效自然保留；undo 完整恢复旧素材状态。

覆盖：
- replace_media：换源语义（path/offline 清零/代理清除/文件名跟随/
  自定义名保留/可选新 metadata）
- ReplaceMediaCommand：apply/undo/redo，undo 恢复完整旧 asset 快照
- 与 AssetUsageIndex 集成：替换后使用位置与剪辑参数不变
- ProjectCommandStack 集成：一次 undo 回滚替换
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.project import EditProject, MediaAsset, replace_media
from src.features.edit.core.commands import (
    ProjectCommandStack,
    ReplaceMediaCommand,
)
from src.features.edit.core.asset_usage_index import build_asset_usage_index


def _asset(asset_id: str, path: str, **overrides) -> MediaAsset:
    params = dict(
        asset_id=asset_id,
        path=path,
        media_type="video",
        display_name=path.rsplit(os.sep, 1)[-1],
        metadata={"duration": 100, "fps": 24.0},
        proxy_path="/tmp/old_proxy.mov",
        proxy_status="ready",
    )
    params.update(overrides)
    return MediaAsset(**params)


def _project(asset: MediaAsset) -> EditProject:
    return EditProject(
        project_id="p1",
        name="proj",
        assets={asset.asset_id: asset},
        root_bin_id="root",
    )


class TestReplaceMediaFunction:
    def test_replaces_path_and_clears_proxy(self, tmp_path) -> None:
        old = tmp_path / "a001.mov"
        new = tmp_path / "b002.mov"
        old.write_bytes(b"x")
        new.write_bytes(b"y")
        project = _project(_asset("a1", str(old)))
        result = replace_media(project, "a1", str(new))

        asset = result.assets["a1"]
        assert asset.path == str(new)
        assert asset.is_offline is False
        assert asset.proxy_path is None
        assert asset.proxy_status == "none"
        # 不可变：原工程不变
        assert project.assets["a1"].path == str(old)

    def test_display_name_follows_filename_only_if_untouched(self, tmp_path) -> None:
        old = tmp_path / "a001.mov"
        new = tmp_path / "b002.mov"
        old.write_bytes(b"x")
        new.write_bytes(b"y")

        # 未改名 → 跟随新文件名
        result = replace_media(_project(_asset("a1", str(old))), "a1", str(new))
        assert result.assets["a1"].display_name == "b002.mov"

        # 用户改过名 → 保留
        renamed = _project(_asset("a1", str(old), display_name="主演特写"))
        result2 = replace_media(renamed, "a1", str(new))
        assert result2.assets["a1"].display_name == "主演特写"

    def test_optional_new_metadata(self, tmp_path) -> None:
        old = tmp_path / "a.mov"; old.write_bytes(b"x")
        new = tmp_path / "b.mov"; new.write_bytes(b"y")
        project = _project(_asset("a1", str(old)))

        kept = replace_media(project, "a1", str(new))
        assert kept.assets["a1"].metadata == {"duration": 100, "fps": 24.0}

        probed = replace_media(
            project, "a1", str(new),
            new_metadata={"duration": 200, "fps": 25.0, "codec": "prores"},
        )
        assert probed.assets["a1"].metadata["duration"] == 200
        assert probed.assets["a1"].metadata["fps"] == 25.0

    def test_missing_target_raises(self, tmp_path) -> None:
        old = tmp_path / "a.mov"; old.write_bytes(b"x")
        project = _project(_asset("a1", str(old)))
        with pytest.raises(ValueError):
            replace_media(project, "a1", str(tmp_path / "ghost.mov"))

    def test_unknown_asset_raises(self, tmp_path) -> None:
        target = tmp_path / "b.mov"; target.write_bytes(b"y")
        with pytest.raises(ValueError):
            replace_media(_project(_asset("a1", str(target))), "ghost", str(target))

    def test_smart_bins_and_bins_preserved(self, tmp_path) -> None:
        old = tmp_path / "a.mov"; old.write_bytes(b"x")
        new = tmp_path / "b.mov"; new.write_bytes(b"y")
        from src.features.edit.core.project import SmartBin
        project = _project(_asset("a1", str(old)))
        project = project.add_smart_bin(SmartBin(
            bin_id="sb", name="n", filter_type="custom", filter_params={},
        ))
        result = replace_media(project, "a1", str(new))
        assert "sb" in result.smart_bins
        assert result.bins == project.bins


class TestReplaceMediaCommand:
    def test_apply_undo_redo(self, tmp_path) -> None:
        old = tmp_path / "a001.mov"; old.write_bytes(b"x")
        new = tmp_path / "b002.mov"; new.write_bytes(b"y")
        project = _project(_asset("a1", str(old), keywords=("k",), rating=4))
        cmd = ReplaceMediaCommand.create("a1", str(new))

        applied = cmd.apply(project)
        assert applied.assets["a1"].path == str(new)

        undone = cmd.undo(applied)
        restored = undone.assets["a1"]
        assert restored.path == str(old)
        assert restored.proxy_path == "/tmp/old_proxy.mov"
        assert restored.proxy_status == "ready"
        assert restored.display_name == "a001.mov"
        assert restored.metadata == {"duration": 100, "fps": 24.0}
        # 日志字段在替换与撤销后都保留
        assert restored.keywords == ("k",)
        assert restored.rating == 4

        redone = cmd.apply(undone)
        assert redone.assets["a1"].path == str(new)
        undone2 = cmd.undo(redone)
        assert undone2.assets["a1"].path == str(old)

    def test_undo_preserves_smart_bins(self, tmp_path) -> None:
        old = tmp_path / "a.mov"; old.write_bytes(b"x")
        new = tmp_path / "b.mov"; new.write_bytes(b"y")
        from src.features.edit.core.project import SmartBin
        project = _project(_asset("a1", str(old)))
        project = project.add_smart_bin(SmartBin(
            bin_id="sb", name="n", filter_type="custom", filter_params={},
        ))
        cmd = ReplaceMediaCommand.create("a1", str(new))
        undone = cmd.undo(cmd.apply(project))
        assert "sb" in undone.smart_bins

    def test_dict_round_trip(self) -> None:
        cmd = ReplaceMediaCommand.create("a1", "/x.mov", new_metadata={"fps": 25.0})
        restored = ReplaceMediaCommand.from_dict(cmd.to_dict())
        assert restored.asset_id == "a1"
        assert restored.new_path == "/x.mov"
        assert restored.new_metadata == {"fps": 25.0}

    def test_stack_single_undo(self, tmp_path) -> None:
        old = tmp_path / "a.mov"; old.write_bytes(b"x")
        new = tmp_path / "b.mov"; new.write_bytes(b"y")
        stack = ProjectCommandStack()
        current = stack.execute(
            ReplaceMediaCommand.create("a1", str(new)), _project(_asset("a1", str(old)))
        )
        assert current.assets["a1"].path == str(new)
        undone = stack.undo(current)
        assert undone.assets["a1"].path == str(old)


class TestClipParametersPreserved:
    def test_usage_index_and_clip_params_unchanged_after_replace(self, tmp_path) -> None:
        """验收：替换源后时间线剪辑引用与入出点完全不变。"""
        from src.features.edit.core.timeline_v2 import (
            ClipInstance, EditTrack, Rational, TimelineSequence,
        )

        old = tmp_path / "a001.mov"; old.write_bytes(b"x")
        new = tmp_path / "b002.mov"; new.write_bytes(b"y")
        project = _project(_asset("a1", str(old)))

        clip = ClipInstance(
            clip_id="c1", asset_id="a1", track_id="v1",
            timeline_start=12, timeline_duration=48,
            source_start=34, source_duration=48,
        )
        seq = TimelineSequence(
            timeline_id="t1", name="T1",
            fps=Rational(24, 1), width=1920, height=1080, sample_rate=48000,
            video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video",
                                    index=0, clips=(clip,)),),
        )

        index_before = build_asset_usage_index([seq], project)
        result = replace_media(project, "a1", str(new))
        index_after = build_asset_usage_index([seq], result)

        assert [u.format() for u in index_after.locations_for("a1")] == \
               [u.format() for u in index_before.locations_for("a1")]
        # 剪辑对象本身未被触碰
        assert seq.video_tracks[0].clips[0].source_start == 34
        assert seq.video_tracks[0].clips[0].timeline_duration == 48
        assert seq.video_tracks[0].clips[0].asset_id == "a1"
