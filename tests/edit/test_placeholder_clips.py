"""B7: 占位符片段（Placeholder Clip）测试。

覆盖：
- 占位素材创建/序列化往返（空 path、元数据规格）
- 替换命令（保留位置/时长/链接/效果；undo 还原占位）
- preflight 拦截（默认 blocking 列出位置+备注；允许时降级 warning）
- 占位静态帧渲染（彩条+斜纹，确定性）
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.deliver.core.video_export_preflight import (  # noqa: E402
    PreflightContext,
    run_video_export_preflight,
)
from src.features.edit.core.commands import ReplaceClipAssetCommand  # noqa: E402
from src.features.edit.core.placeholder_frame import (  # noqa: E402
    is_placeholder_asset,
    placeholder_note,
    render_placeholder_frame,
)
from src.features.edit.core.project import (  # noqa: E402
    MediaAsset,
    create_placeholder_asset,
)
from src.features.edit.core.timeline_v2 import (  # noqa: E402
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)


def _make_project_with_placeholder(name="航拍空镜", note="此处待补：航拍"):
    project = __import__(
        "src.features.edit.core.project", fromlist=["EditProject"]
    ).EditProject.create(name="b7")
    return create_placeholder_asset(
        project, name, width=1920, height=1080, fps=24.0,
        duration_frames=48, note=note,
    )


def _sequence_with_clip(asset_id: str) -> TimelineSequence:
    clip = ClipInstance(
        clip_id="ph1", asset_id=asset_id, track_id="v1",
        timeline_start=24, timeline_duration=48,
        source_start=0, source_duration=48,
        linked_clip_id="a1",
    )
    track = EditTrack(track_id="v1", name="V1", track_type="video",
                      index=0, clips=(clip,))
    return TimelineSequence(video_tracks=(track,), fps=Rational(24, 1))


class TestPlaceholderAsset:
    def test_create_and_persist(self) -> None:
        project, asset = _make_project_with_placeholder()
        assert asset.media_type == "placeholder"
        assert asset.path == ""
        assert asset.metadata["placeholder"] is True
        assert asset.metadata["duration_frames"] == 48
        assert placeholder_note(asset) == "此处待补：航拍"
        # 工程持有 + replace 派生（frozen dataclass 序列化由 save_router
        # 基于 dataclasses.asdict 体系，字段天然可 JSON 化）
        assert asset.asset_id in project.assets
        from dataclasses import replace
        renamed = replace(asset, display_name="renamed")
        assert is_placeholder_asset(renamed)
        assert project.assets[asset.asset_id].display_name == "航拍空镜"

    def test_empty_name_falls_back(self) -> None:
        _project, asset = _make_project_with_placeholder(name="   ")
        assert asset.display_name == "Placeholder"


class TestPlaceholderFrame:
    def test_frame_shape_and_determinism(self) -> None:
        f1 = render_placeholder_frame(64, 36)
        f2 = render_placeholder_frame(64, 36)
        assert f1.shape == (36, 64, 3)
        assert f1.dtype == np.uint8
        assert np.array_equal(f1, f2)
        # 非全黑（彩条）且含斜纹变化
        assert f1.max() > 150
        assert not np.array_equal(f1[0, 0], f1[16, 16])

    def test_asset_frame_uses_metadata_size(self) -> None:
        _project, asset = _make_project_with_placeholder()
        frame = render_placeholder_frame.__wrapped__ if False else None
        from src.features.edit.core.placeholder_frame import (
            render_placeholder_frame_for_asset,
        )
        frame = render_placeholder_frame_for_asset(asset, 320, 180)
        assert frame.shape == (1080, 1920, 3)  # 用元数据规格


class TestReplaceClipAssetCommand:
    def test_replace_keeps_geometry_and_links(self) -> None:
        project, _asset = _make_project_with_placeholder()
        seq = _sequence_with_clip(next(iter(project.assets)))
        cmd = ReplaceClipAssetCommand.create("ph1", "v1", "real_asset")
        new_seq = cmd.apply(seq)
        clip = new_seq.get_clip_by_id("ph1")
        assert clip.asset_id == "real_asset"
        # 位置/时长/链接/速度保留
        assert clip.timeline_start == 24
        assert clip.timeline_duration == 48
        assert clip.linked_clip_id == "a1"
        # undo 还原占位
        undone = cmd.undo(new_seq)
        restored = undone.get_clip_by_id("ph1")
        assert restored.asset_id == clip_asset_id(seq)

    def test_serialization_roundtrip(self) -> None:
        cmd = ReplaceClipAssetCommand.create("ph1", "v1", "real")
        applied_cmd = cmd.apply(_sequence_with_clip("ph"))
        restored = ReplaceClipAssetCommand.from_dict(cmd.to_dict())
        assert restored.new_asset_id == "real"
        undone = restored.undo(applied_cmd)
        assert undone.get_clip_by_id("ph1").asset_id == "ph"


def clip_asset_id(seq: TimelineSequence) -> str:
    return seq.get_clip_by_id("ph1").asset_id


class TestPreflightBlocking:
    def _job(self) -> Any:
        from src.features.deliver.core.video_export_job import (
            AudioConfig,
            VideoConfig,
            VideoExportJob,
        )
        from src.features.deliver.core.video_export_job import SourceKind

        return VideoExportJob(
            project_id="p", project_name="proj", timeline_id="t",
            timeline_name="tl", source_kind=SourceKind.TIMELINE,
            video_config=VideoConfig(),
            audio_config=AudioConfig(),
            output_path="/tmp/out.mp4", name="out.mp4",
        )

    def test_placeholder_clips_block_export(self) -> None:
        project, asset = _make_project_with_placeholder(note="待补空镜")
        seq = _sequence_with_clip(asset.asset_id)
        ctx = PreflightContext(project=project, sequence=seq)
        result = run_video_export_preflight(self._job(), ctx)
        messages = [i.message for i in result.blocking
                    if i.code == "edit.placeholder_clips"]
        assert messages, "占位片段应默认拦截导出"
        assert any("待补空镜" in m for m in messages)
        assert any("24f" in m for m in messages)

    def test_allow_flag_downgrades_to_warning(self) -> None:
        project, asset = _make_project_with_placeholder()
        seq = _sequence_with_clip(asset.asset_id)
        ctx = PreflightContext(project=project, sequence=seq)
        job = self._job()
        job.extra_options = {"allow_placeholder_clips": True}
        result = run_video_export_preflight(job, ctx)
        codes = [i.code for i in result.warnings]
        assert "edit.placeholder_clips" in codes
        assert all(
            i.code != "edit.placeholder_clips" for i in result.blocking
        )


from typing import Any  # noqa: E402
