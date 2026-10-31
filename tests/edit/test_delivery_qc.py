"""Delivery QC（P1-19）测试。

验收口径（计划表）：
- 导出前得到 blocking/warning 分级；blocking 中止导出；
- 离线素材、音画时长差、字幕越界、空时间线、帧率不一致均有稳定 check_id；
- 导出后文件级检查（存在/非空/SHA-256）可独立调用并入归档。
"""

from __future__ import annotations

import json

import pytest

from src.features.edit.core.delivery_qc import (
    DEFAULT_AV_TOLERANCE_FRAMES,
    DeliveryQCReport,
    QCSeverity,
    inspect_output_file,
    run_delivery_qc,
)
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    SubtitleClip,
    TimelineSequence,
)


def _asset(
    asset_id: str = "asset-1",
    path: str = "/tmp/does-not-exist-source.mp4",
    offline: bool = False,
    fps: float | None = 24.0,
) -> MediaAsset:
    metadata: dict = {}
    if fps is not None:
        metadata["fps"] = fps
    return MediaAsset(
        asset_id=asset_id,
        path=path,
        media_type="video",
        display_name="Source.mp4",
        metadata=metadata,
        is_offline=offline,
    )


def _clip(
    clip_id: str,
    track_id: str,
    start: int,
    duration: int,
    asset_id: str = "asset-1",
) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=asset_id,
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=start,
        source_duration=duration,
    )


def _sequence(
    video_clips: tuple[ClipInstance, ...] = (),
    audio_clips: tuple[ClipInstance, ...] = (),
    subtitle_clips: tuple[SubtitleClip, ...] = (),
) -> TimelineSequence:
    return TimelineSequence(
        timeline_id="seq-qc",
        name="QC",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=(
            EditTrack(
                track_id="v1", name="V1", track_type="video", index=0,
                clips=video_clips,
            ),
        ),
        audio_tracks=(
            EditTrack(
                track_id="a1", name="A1", track_type="audio", index=0,
                clips=audio_clips,
            ),
        ),
        subtitle_tracks=(
            EditTrack(
                track_id="s1", name="S1", track_type="subtitle", index=0,
                subtitle_clips=subtitle_clips,
            ),
        ) if subtitle_clips else (),
    )


def _project(*assets: MediaAsset) -> EditProject:
    project = EditProject(project_id="proj-qc", name="QC")
    for asset in assets:
        project.assets[asset.asset_id] = asset
    return project


def _sub(start: int, duration: int, text: str = "hello") -> SubtitleClip:
    return SubtitleClip(
        clip_id="sub-1",
        track_id="s1",
        timeline_start=start,
        timeline_duration=duration,
        text=text,
    )


@pytest.fixture()
def healthy_setup(tmp_path):
    media = tmp_path / "source.mp4"
    media.write_bytes(b"0" * 64)
    asset = _asset(path=str(media))
    sequence = _sequence(
        video_clips=(_clip("c1", "v1", 0, 96),),
        audio_clips=(_clip("a-c1", "a1", 0, 96),),
    )
    return sequence, _project(asset)


class TestHealthyTimeline:
    def test_no_findings_for_healthy_timeline(self, healthy_setup):
        sequence, project = healthy_setup
        report = run_delivery_qc(sequence, project)
        assert report.passed
        assert report.findings == ()

    def test_report_metadata(self, healthy_setup):
        sequence, project = healthy_setup
        report = run_delivery_qc(sequence, project)
        assert isinstance(report, DeliveryQCReport)
        assert report.checked_at
        assert report.to_dict()["schema"] == "topos.delivery_qc.v1"
        json.dumps(report.to_dict())


class TestOfflineMedia:
    def test_offline_asset_blocks(self, healthy_setup):
        sequence, project = healthy_setup
        offline = _asset(offline=True)
        project.assets[offline.asset_id] = offline
        report = run_delivery_qc(sequence, project)
        assert not report.passed
        assert report.blocking_messages and any(
            "离线" in m for m in report.blocking_messages
        )

    def test_missing_file_blocks(self, healthy_setup):
        sequence, project = healthy_setup
        ghost = _asset(asset_id="asset-2", path="/definitely/missing/file.mov")
        sequence = _sequence(
            video_clips=(_clip("c2", "v1", 0, 48, asset_id="asset-2"),)
        )
        report = run_delivery_qc(sequence, project)
        assert not report.passed
        assert any(f.check_id == "media.offline" for f in report.blocking)

    def test_virtual_nested_asset_not_blocked(self, healthy_setup):
        """P1-02 回归：嵌套序列等宿主虚拟素材无真实源文件，QC 不得阻断。

        真实素材驱动测试抓到的缺陷：嵌套片段 path 为 topos:// 虚拟 URI，
        _check_offline_media 按"源文件不存在"报 BLOCKING，导致含嵌套的
        工程完全无法导出。
        """
        sequence, project = healthy_setup
        nested = _asset(asset_id="asset-nest", path="topos://sequence/child")
        from dataclasses import replace
        nested = replace(nested, media_type="nested_sequence")
        project.assets[nested.asset_id] = nested
        sequence = _sequence(
            video_clips=(_clip("cn", "v1", 0, 48, asset_id="asset-nest"),)
        )
        report = run_delivery_qc(sequence, project)
        assert report.passed, report.blocking_messages
        assert not any(
            f.check_id == "media.offline" for f in report.blocking
        )

    def test_unknown_asset_blocks(self, healthy_setup):
        sequence, project = healthy_setup
        sequence = _sequence(
            video_clips=(_clip("c3", "v1", 0, 48, asset_id="ghost"),)
        )
        report = run_delivery_qc(sequence, project)
        assert not report.passed
        assert any(f.check_id == "media.offline" for f in report.blocking)

    def test_no_project_skips_media_check(self, healthy_setup):
        """project=None 时无法解析引用，跳过素材检查（不产生噪声阻断）。"""
        sequence, _ = healthy_setup
        report = run_delivery_qc(sequence, None)
        assert report.passed
        assert not any(f.check_id == "media.offline" for f in report.findings)

    def test_offline_details_reference_clip_and_track(self, healthy_setup):
        sequence, project = healthy_setup
        project.assets["asset-1"] = _asset(offline=True)
        report = run_delivery_qc(sequence, project)
        finding = next(f for f in report.blocking if f.check_id == "media.offline")
        assert finding.details["asset_id"] == "asset-1"
        assert finding.details["track_id"] == "v1"


class TestAVDuration:
    def test_mismatch_warns(self, healthy_setup):
        sequence, project = healthy_setup
        sequence = _sequence(
            video_clips=(_clip("c1", "v1", 0, 96),),
            audio_clips=(_clip("a1", "a1", 0, 96 - 10),),
        )
        report = run_delivery_qc(sequence, project)
        assert report.passed  # 仅警告
        warnings = [f for f in report.warnings if f.check_id == "av.duration_mismatch"]
        assert len(warnings) == 1
        assert warnings[0].details["video_end_frames"] == 96

    def test_small_diff_within_tolerance(self, healthy_setup):
        sequence, project = healthy_setup
        sequence = _sequence(
            video_clips=(_clip("c1", "v1", 0, 96),),
            audio_clips=(_clip("a1", "a1", 0, 96 - DEFAULT_AV_TOLERANCE_FRAMES),),
        )
        report = run_delivery_qc(sequence, project)
        assert not any(
            f.check_id == "av.duration_mismatch" for f in report.findings
        )

    def test_video_only_does_not_warn(self, healthy_setup):
        sequence, project = healthy_setup
        sequence = _sequence(video_clips=(_clip("c1", "v1", 0, 96),))
        report = run_delivery_qc(sequence, project)
        assert not any(
            f.check_id == "av.duration_mismatch" for f in report.findings
        )


class TestSubtitleBounds:
    def test_beyond_duration_warns(self, healthy_setup):
        sequence, project = healthy_setup
        sequence = _sequence(
            video_clips=(_clip("c1", "v1", 0, 96),),
            subtitle_clips=(_sub(90, 48),),
        )
        report = run_delivery_qc(sequence, project)
        assert report.passed
        subs = [f for f in report.warnings if f.check_id == "subtitle.beyond_duration"]
        assert len(subs) == 1
        assert subs[0].details["end"] == 138

    def test_in_bounds_subtitle_clean(self, healthy_setup):
        sequence, project = healthy_setup
        sequence = _sequence(
            video_clips=(_clip("c1", "v1", 0, 96),),
            subtitle_clips=(_sub(0, 96),),
        )
        report = run_delivery_qc(sequence, project)
        assert not any(
            f.check_id == "subtitle.beyond_duration" for f in report.findings
        )


class TestEmptyTimeline:
    def test_empty_timeline_warns_not_blocks(self):
        report = run_delivery_qc(_sequence())
        assert report.passed
        assert any(f.check_id == "timeline.empty" for f in report.warnings)


class TestFpsMismatch:
    def test_asset_fps_mismatch_warns(self, healthy_setup):
        sequence, project = healthy_setup
        asset = _asset(fps=29.97)
        project.assets["asset-1"] = asset
        report = run_delivery_qc(sequence, project)
        found = [f for f in report.warnings if f.check_id == "media.fps_mismatch"]
        assert len(found) == 1
        assert found[0].details["timeline_fps"] == pytest.approx(24.0)

    def test_matching_fps_clean(self, healthy_setup):
        sequence, project = healthy_setup
        report = run_delivery_qc(sequence, project)
        assert not any(
            f.check_id == "media.fps_mismatch" for f in report.findings
        )


class TestOutputFileInspection:
    def test_missing_output_blocks(self):
        findings = inspect_output_file("/definitely/not/here.mp4")
        assert any(
            f.severity is QCSeverity.BLOCKING and f.check_id == "output.missing"
            for f in findings
        )

    def test_empty_output_blocks(self, tmp_path):
        empty = tmp_path / "out.mp4"
        empty.write_bytes(b"")
        findings = inspect_output_file(str(empty))
        assert any(f.check_id == "output.empty" for f in findings)

    def test_valid_output_records_sha256(self, tmp_path):
        out = tmp_path / "out.mp4"
        out.write_bytes(b"payload-data")
        findings = inspect_output_file(str(out))
        info = next(f for f in findings if f.check_id == "output.integrity")
        assert info.severity is QCSeverity.INFO
        assert len(info.details["sha256"]) == 64
        assert info.details["size"] == 12

    def test_empty_path_no_findings(self):
        assert inspect_output_file("") == []

    def test_output_path_argument_integrated(self, tmp_path, healthy_setup):
        sequence, project = healthy_setup
        out = tmp_path / "final.mp4"
        out.write_bytes(b"x" * 32)
        report = run_delivery_qc(sequence, project, output_path=str(out))
        assert report.passed
        assert any(f.check_id == "output.integrity" for f in report.infos)


class TestExporterGate:
    """P1-19: export_timeline 集成——QC BLOCKING 阻断且不产出文件。"""

    def test_export_blocked_by_offline_media(self, tmp_path):
        from src.features.edit.core.timeline_export import (
            TimelineExportConfig,
            TimelineExporter,
        )
        asset = _asset(path="/definitely/missing-source.mov", offline=True)
        sequence = _sequence(
            video_clips=(_clip("c1", "v1", 0, 24, asset_id="asset-1"),)
        )
        exporter = TimelineExporter(_project(asset), sequence, frame_provider=None)
        config = TimelineExportConfig(
            timeline_id=sequence.timeline_id,
            output_path=str(tmp_path / "out.mov"),
            include_audio=False,
        )
        with pytest.raises(ValueError, match="Delivery QC"):
            exporter.export_timeline(config)
        assert exporter.last_qc_report is not None
        assert not exporter.last_qc_report.passed
        assert not (tmp_path / "out.mov").exists()

    def test_mock_project_does_not_false_block(self, tmp_path):
        """MagicMock/鸭子类型工程（图校验等测试用）不得被 QC 误阻断。"""
        from unittest.mock import MagicMock

        from src.features.edit.core.timeline_export import (
            TimelineExportConfig,
            TimelineExporter,
        )
        clip = ClipInstance(
            clip_id="c1", asset_id="mock-asset", track_id="v1",
            timeline_start=0, timeline_duration=24,
            source_start=0, source_duration=24,
        )
        sequence = TimelineSequence(
            fps=Rational(24, 1),
            video_tracks=(
                EditTrack(track_id="v1", track_type="video", index=0, clips=(clip,)),
            ),
        )
        exporter = TimelineExporter(MagicMock(), sequence, MagicMock())
        config = TimelineExportConfig(
            timeline_id=sequence.timeline_id,
            output_path=str(tmp_path / "out.mov"),
            include_audio=False,
        )
        try:
            exporter.export_timeline(config)
        except ValueError as exc:
            assert "Delivery QC" not in str(exc)
        assert exporter.last_qc_report is not None
        assert exporter.last_qc_report.passed
