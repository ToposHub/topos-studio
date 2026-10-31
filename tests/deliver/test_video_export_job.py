"""Tests for the VideoExportJob snapshot model.

Covers:
* Default construction + auto-generated fields (job_id, name).
* Structural validation (range / resolution / output extension / codec).
* JSON round-trip via to_dict / from_dict (incl. unknown-field tolerance).
* Stale RUNNING demotion on load.
* Bridge to legacy ExportJob (to_legacy_job / from_legacy_job) — both the
  round-trip path and the migration-from-flat-config path.
* Bridge to TimelineExportConfig (only when the edit feature is importable).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.deliver.core.export_queue import ExportJob, JobStatus, clear_export_queue
from src.features.deliver.core.video_export_job import (
    AudioConfig,
    FilenameUse,
    NamingConfig,
    OverwritePolicy,
    RangeType,
    SourceKind,
    SubtitleConfig,
    SubtitleMode,
    VIDEO_EXPORT_JOB_SCHEMA_VERSION,
    VideoConfig,
    VideoExportJob,
    VideoJobStatus,
    RenderMode,
)


@pytest.fixture(autouse=True)
def isolated_queue_path(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOS_CONFIG_PATH", str(tmp_path))
    clear_export_queue()
    yield
    clear_export_queue()


# ── construction ──


class TestConstruction:
    def test_defaults(self):
        job = VideoExportJob()
        assert job.job_id
        assert job.schema_version == VIDEO_EXPORT_JOB_SCHEMA_VERSION
        assert job.status == VideoJobStatus.PENDING
        assert job.source_kind == SourceKind.TIMELINE
        assert job.range_type == RangeType.ENTIRE
        assert isinstance(job.video_config, VideoConfig)
        assert isinstance(job.audio_config, AudioConfig)
        assert isinstance(job.subtitle_config, SubtitleConfig)
        assert job.overwrite_policy == OverwritePolicy.ASK

    def test_auto_name_from_output_path(self):
        job = VideoExportJob(output_path="/tmp/film/master.mov")
        assert job.name == "master.mov"

    def test_auto_name_from_timeline(self):
        job = VideoExportJob(timeline_name="Main Cut")
        assert job.name == "Main Cut"

    def test_empty_job_id_is_regenerated(self):
        job = VideoExportJob(job_id="")
        assert job.job_id  # not empty


# ── validation ──


class TestValidation:
    def test_valid_job_no_errors(self):
        job = VideoExportJob(
            timeline_id="tl1",
            output_path="/tmp/out.mov",
            video_config=VideoConfig(codec="prores_422", container="mov"),
        )
        assert job.validate() == []

    def test_missing_timeline_blocks(self):
        job = VideoExportJob(source_kind=SourceKind.TIMELINE, output_path="/tmp/x.mov")
        errors = job.validate()
        assert any("timeline_id" in e for e in errors)

    def test_invalid_range_blocks(self):
        job = VideoExportJob(
            timeline_id="tl1",
            range_type=RangeType.CUSTOM,
            start_frame=100,
            end_frame=50,
            output_path="/tmp/x.mov",
        )
        errors = job.validate()
        assert any("end_frame" in e for e in errors)

    def test_wrong_extension_blocks(self):
        job = VideoExportJob(
            timeline_id="tl1",
            output_path="/tmp/out.txt",
            video_config=VideoConfig(codec="h264", container="mp4"),
        )
        errors = job.validate()
        assert any("extension" in e for e in errors)

    def test_image_sequence_directory_ok(self):
        job = VideoExportJob(
            timeline_id="tl1",
            output_path="/tmp/seq",  # directory-style
            video_config=VideoConfig(codec="png_sequence", container=""),
        )
        assert job.validate() == []

    def test_bad_resolution_blocks(self):
        job = VideoExportJob(
            timeline_id="tl1",
            output_path="/tmp/x.mov",
            video_config=VideoConfig(resolution=(0, 1080)),
        )
        assert any("resolution" in e for e in job.validate())

    def test_individual_filename_digits_are_bounded(self):
        job = VideoExportJob(
            timeline_id="tl1", output_path="/tmp/x.mov",
            naming_config=NamingConfig(unique_filename_digits=1),
        )
        assert "unique_filename_digits must be between 2 and 10" in job.validate()


# ── serialisation ──


class TestSerialisation:
    def test_roundtrip(self):
        job = VideoExportJob(
            project_id="p1",
            project_revision=7,
            timeline_id="tl1",
            timeline_name="Main",
            output_path="/tmp/x.mov",
            video_config=VideoConfig(
                codec="prores_4444", container="mov", resolution=(3840, 2160),
                fps=25.0, bit_depth=10, alpha=True, quality_mode="auto",
            ),
            audio_config=AudioConfig(codec="pcm_s24le", sample_rate=96000, bitrate=2304),
            subtitle_config=SubtitleConfig(mode=SubtitleMode.BURN, track_id="sub1"),
            naming_config=NamingConfig(
                render_mode=RenderMode.INDIVIDUAL_CLIPS,
                filename_use=FilenameUse.SOURCE,
                unique_filename_digits=8,
                frame_handles=25,
            ),
            overwrite_policy=OverwritePolicy.AUTO_NUMBER,
        )
        d = job.to_dict()
        recovered = VideoExportJob.from_dict(d)
        assert recovered.job_id == job.job_id
        assert recovered.timeline_name == job.timeline_name
        assert recovered.video_config.codec == "prores_4444"
        assert recovered.video_config.resolution == (3840, 2160)
        assert recovered.video_config.alpha is True
        assert recovered.audio_config.sample_rate == 96000
        assert recovered.subtitle_config.mode == SubtitleMode.BURN
        assert recovered.naming_config.unique_filename_digits == 8
        assert recovered.naming_config.frame_handles == 25
        assert recovered.overwrite_policy == OverwritePolicy.AUTO_NUMBER

    def test_unknown_fields_preserved(self):
        d = {
            "job_id": "abc",
            "timeline_id": "t1",
            "output_path": "/tmp/x.mov",
            "future_field": {"nested": 42},
        }
        job = VideoExportJob.from_dict(d)
        assert job._unknown_fields.get("future_field") == {"nested": 42}
        # And it survives another round-trip
        d2 = job.to_dict()
        assert d2["unknown_fields"]["future_field"] == {"nested": 42}

    def test_stale_running_demoted_on_load(self):
        d = VideoExportJob(
            timeline_id="t1", output_path="/tmp/x.mov",
        ).to_dict()
        d["status"] = VideoJobStatus.RUNNING
        d["progress"] = 0.5
        job = VideoExportJob.from_dict(d)
        assert job.status == VideoJobStatus.PENDING
        assert job.progress == 0.0


# ── legacy bridge ──


class TestLegacyBridge:
    def test_to_legacy_and_back(self):
        job = VideoExportJob(
            timeline_id="tl1",
            timeline_name="Main",
            output_path="/tmp/out.mov",
            video_config=VideoConfig(codec="h264", container="mp4", resolution=(1920, 1080)),
            status=VideoJobStatus.PENDING,
        )
        legacy = job.to_legacy_job()
        assert legacy.job_id == job.job_id
        assert legacy.output_path == "/tmp/out.mov"
        assert legacy.status == JobStatus.PENDING
        assert legacy.config["video_job"]["timeline_id"] == "tl1"

        recovered = VideoExportJob.from_legacy_job(legacy)
        assert recovered.job_id == job.job_id
        assert recovered.video_config.codec == "h264"
        assert recovered.video_config.resolution == (1920, 1080)

    def test_from_flat_config_migration(self):
        legacy = ExportJob(
            job_id="legacy1",
            name="legacy.mov",
            config={
                "timeline_id": "tl1",
                "timeline_name": "Old",
                "output_path": "/tmp/legacy.mov",
                "codec": "prores_422",
                "container": "mov",
            },
            output_path="/tmp/legacy.mov",
        )
        job = VideoExportJob.from_legacy_job(legacy)
        assert job.timeline_id == "tl1"
        assert job.video_config.codec == "prores_422"
        assert job.video_config.container == "mov"

    def test_needs_relink_preserved_through_legacy(self):
        job = VideoExportJob(
            timeline_id="t1",
            output_path="/tmp/x.mov",
            status=VideoJobStatus.NEEDS_RELINK,
        )
        legacy = job.to_legacy_job()
        # The queue now natively supports NEEDS_RELINK (so it won't auto-run
        # via _process_next, which only picks PENDING).
        assert legacy.status == JobStatus.NEEDS_RELINK
        # And it round-trips back to the typed view.
        recovered = VideoExportJob.from_legacy_job(legacy)
        assert recovered.status == VideoJobStatus.NEEDS_RELINK


# ── TimelineExportConfig bridge ──


class TestTimelineExportConfigBridge:
    def test_bitrate_mode_is_preserved(self):
        job = VideoExportJob(
            timeline_id="tl1",
            output_path="/tmp/x.mp4",
            video_config=VideoConfig(
                codec="h264",
                container="mp4",
                quality_mode="bitrate",
                quality_value=12_000_000_000,
            ),
        )

        cfg = job.to_timeline_export_config()

        assert cfg.quality_mode == "bitrate"
        assert cfg.quality == 12_000_000_000

    def test_mapping_roundtrip(self):
        try:
            from src.features.edit.core.timeline_export import (
                ExportCodec,
                ExportRangeType,
                ExportSource,
            )
        except ImportError:
            pytest.skip("edit feature unavailable")

        job = VideoExportJob(
            timeline_id="tl1",
            source_kind=SourceKind.TIMELINE,
            range_type=RangeType.CUSTOM,
            start_frame=10,
            end_frame=100,
            output_path="/tmp/x.mp4",
            video_config=VideoConfig(codec="h264", container="mp4", resolution=(1920, 1080), fps=25.0, quality_value=20),
            audio_config=AudioConfig(include_audio=True, codec="aac", sample_rate=48000, bitrate=192),
            subtitle_config=SubtitleConfig(mode=SubtitleMode.BURN),
            naming_config=NamingConfig(
                render_mode=RenderMode.INDIVIDUAL_CLIPS,
                filename_use=FilenameUse.SOURCE,
                unique_filename_digits=6,
                frame_handles=25,
            ),
        )
        cfg = job.to_timeline_export_config()
        assert cfg.source == ExportSource.TIMELINE
        assert cfg.range_type == ExportRangeType.CUSTOM
        assert cfg.start_frame == 10 and cfg.end_frame == 100
        assert cfg.codec == ExportCodec.H264
        assert cfg.resolution == (1920, 1080)
        assert cfg.fps == 25.0
        assert cfg.quality == 20
        assert cfg.audio_codec == "aac"
        assert cfg.burn_subtitles is True
        assert cfg.render_mode == "individual_clips"
        assert cfg.filename_use == "source"
        assert cfg.unique_filename_digits == 6
        assert cfg.frame_handles == 25

    def test_unknown_codec_raises(self):
        job = VideoExportJob(
            timeline_id="t1",
            output_path="/tmp/x.mov",
            video_config=VideoConfig(codec="nope", container="mov"),
        )
        with pytest.raises(ValueError):
            job.to_timeline_export_config()

    # ── HEVC 8-bit 交付档透传（2026-09-16 平面直通扩展）──

    def test_hevc_pixel_format_forwarded_in_domain(self):
        job = VideoExportJob(
            timeline_id="t1",
            output_path="/tmp/x.mp4",
            video_config=VideoConfig(
                codec="hevc", container="mp4", pixel_format="yuv420p"),
        )
        assert job.to_timeline_export_config().pixel_format == "yuv420p"

    def test_hevc_main10_profile_preserves_legacy_10bit_intent(self):
        """pixel_format 曾是 HEVC 不可编辑的占位值，main10 语义优先。"""
        job = VideoExportJob(
            timeline_id="t1",
            output_path="/tmp/x.mp4",
            video_config=VideoConfig(
                codec="hevc", container="mp4",
                pixel_format="yuv420p", profile="main10"),
        )
        assert job.to_timeline_export_config().pixel_format == "yuv420p10le"

    def test_hevc_out_of_domain_pixel_format_dropped(self):
        job = VideoExportJob(
            timeline_id="t1",
            output_path="/tmp/x.mp4",
            video_config=VideoConfig(
                codec="hevc", container="mp4", pixel_format="gbrp12le"),
        )
        assert job.to_timeline_export_config().pixel_format == ""

    def test_non_hevc_pixel_format_not_forwarded(self):
        """h264 等其他 codec 的 pixel_format 占位值不透传（行为不变）。"""
        job = VideoExportJob(
            timeline_id="t1",
            output_path="/tmp/x.mp4",
            video_config=VideoConfig(
                codec="h264", container="mp4", pixel_format="yuv420p"),
        )
        assert job.to_timeline_export_config().pixel_format == ""


# ── status helpers ──


class TestStatusHelpers:
    def test_is_terminal(self):
        assert VideoExportJob(status=VideoJobStatus.DONE).is_terminal
        assert VideoExportJob(status=VideoJobStatus.FAILED).is_terminal
        assert VideoExportJob(status=VideoJobStatus.CANCELLED).is_terminal
        assert not VideoExportJob(status=VideoJobStatus.PENDING).is_terminal
        assert not VideoExportJob(status=VideoJobStatus.RUNNING).is_terminal

    def test_is_restorable(self):
        for s in (VideoJobStatus.PENDING, VideoJobStatus.RUNNING,
                  VideoJobStatus.FAILED, VideoJobStatus.NEEDS_RELINK):
            assert VideoExportJob(status=s).is_restorable
        assert not VideoExportJob(status=VideoJobStatus.DONE).is_restorable
