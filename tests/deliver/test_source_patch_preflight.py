"""F2.1 · Source Patch 声道映射入队预检——越界 BLOCKING / 其余放行。

与 ``UpdateSourcePatchCommand.apply`` 同一校验谓词前置到 Deliver 入队：
已知声道数的越界映射在任务运行前拒绝；无映射 / 声道数未知 /
纯视频任务放行（渲染期布局不符回退直通并留警告，语义已收口）。
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from src.features.deliver.core.video_export_preflight import (
    PreflightContext,
    PreflightResult,
    _check_source_patch,
)
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.source_patch import SourcePatchSpec
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)


def _sequence() -> TimelineSequence:
    clip = ClipInstance("c1", "a51", "a0", 0, 24, 0, 24)
    track = EditTrack("a0", "A1", "audio", 0, clips=(clip,))
    return TimelineSequence(
        timeline_id="seq", name="S", width=64, height=48,
        fps=Rational(24, 1), audio_tracks=(track,))


def _project(spec: SourcePatchSpec | None) -> EditProject:
    asset = MediaAsset(
        "a51", "/tmp/x.wav", "audio", "x51",
        metadata={"audio_info": {"channels": 6, "channel_layout": "5.1"}})
    project = EditProject("p", "P", assets={"a51": asset})
    if spec is not None:
        project = project.with_source_patch_override("a51", spec)
    return project


def _ctx(project, sequence) -> PreflightContext:
    return PreflightContext(project=project, sequence=sequence)


def _job(include_audio: bool = True) -> Mock:
    job = Mock(spec=["include_audio"])
    job.include_audio = include_audio
    return job


class TestSourcePatchPreflight:
    def test_out_of_range_map_is_blocking(self) -> None:
        """源声道 6 越界（6 声道源）——入队前拒绝。"""
        project = _project(SourcePatchSpec(audio_channel_map=((6, 0),)))
        result = PreflightResult()
        _check_source_patch(_job(), _ctx(project, _sequence()), result)
        assert not result.ok
        assert any(
            i.code == "source_patch.invalid_map" and i.severity == "blocking"
            for i in result.issues)

    def test_valid_map_passes(self) -> None:
        """5.1 单选 C（源 2）合法——无阻断无警告。"""
        project = _project(SourcePatchSpec(audio_channel_map=((2, 0),)))
        result = PreflightResult()
        _check_source_patch(_job(), _ctx(project, _sequence()), result)
        assert result.ok and not result.issues

    def test_no_override_passes(self) -> None:
        result = PreflightResult()
        _check_source_patch(_job(), _ctx(_project(None), _sequence()), result)
        assert result.ok and not result.issues

    def test_unknown_channel_count_passes(self) -> None:
        """素材无探测元数据——不判越界（渲染期回退直通+警告）。"""
        asset = MediaAsset("a51", "/tmp/x.wav", "audio", "x51", metadata={})
        project = EditProject("p", "P", assets={"a51": asset})
        project = project.with_source_patch_override(
            "a51", SourcePatchSpec(audio_channel_map=((99, 0),)))
        result = PreflightResult()
        _check_source_patch(_job(), _ctx(project, _sequence()), result)
        assert result.ok

    def test_video_only_job_skips(self) -> None:
        project = _project(SourcePatchSpec(audio_channel_map=((6, 0),)))
        result = PreflightResult()
        _check_source_patch(
            _job(include_audio=False), _ctx(project, _sequence()), result)
        assert result.ok and not result.issues

    def test_override_on_unused_asset_ignored(self) -> None:
        """覆盖挂在时间线未引用的素材上——与本任务无关，不阻断。"""
        project = _project(SourcePatchSpec(audio_channel_map=((6, 0),)))
        other_seq = TimelineSequence(
            timeline_id="seq2", name="S2", width=64, height=48,
            fps=Rational(24, 1),
            audio_tracks=(EditTrack("a9", "A9", "audio", 0),))
        result = PreflightResult()
        _check_source_patch(_job(), _ctx(project, other_seq), result)
        assert result.ok
