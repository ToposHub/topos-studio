from pathlib import Path

from src.features.deliver.core.video_export_formats import (
    allowed_codecs_for_container,
    is_codec_container_compatible,
)
from src.features.deliver.core.video_export_job import (
    AudioConfig,
    VideoConfig,
    VideoExportJob,
)
from src.features.deliver.core.video_export_preflight import (
    PreflightContext,
    run_video_export_preflight,
)
from src.features.edit.core.timeline_export import ExportCodec, TimelineExportConfig


def test_gif_is_a_deliver_codec_and_round_trips_palette_settings(tmp_path: Path) -> None:
    assert allowed_codecs_for_container("gif") == ["gif"]
    assert is_codec_container_compatible("gif", "gif")

    job = VideoExportJob(
        timeline_id="timeline-1",
        output_path=str(tmp_path / "preview.gif"),
        video_config=VideoConfig(
            container="gif",
            codec="gif",
            resolution=(640, 360),
            fps=15.0,
            gif_color_count=64,
        ),
        audio_config=AudioConfig(include_audio=False),
    )
    restored = VideoExportJob.from_dict(job.to_dict())
    assert restored.video_config.gif_color_count == 64
    config = restored.to_timeline_export_config()
    assert config.codec is ExportCodec.GIF
    assert config.resolution == (640, 360)
    assert config.fps == 15.0
    assert config.gif_color_count == 64
    assert config.validate() == []


def test_gif_rejects_invalid_palette_and_frame_rate(tmp_path: Path) -> None:
    config = TimelineExportConfig(
        timeline_id="timeline-1",
        codec=ExportCodec.GIF,
        resolution=(320, 180),
        fps=120.0,
        gif_color_count=257,
        output_path=str(tmp_path / "preview.gif"),
    )
    errors = config.validate()
    assert "GIF color count must be between 2 and 256" in errors
    assert "GIF frame rate cannot exceed 100 fps" in errors


def test_gif_audio_is_explicitly_reported_as_unsupported(tmp_path: Path) -> None:
    job = VideoExportJob(
        timeline_id="timeline-1",
        output_path=str(tmp_path / "preview.gif"),
        video_config=VideoConfig(container="gif", codec="gif", fps=12.0),
        audio_config=AudioConfig(include_audio=True),
    )
    result = run_video_export_preflight(
        job, PreflightContext(project=object(), sequence=object())
    )
    assert result.ok
    assert any(issue.code == "gif.audio_unsupported" for issue in result.warnings)


def test_timeline_encoder_factory_dispatches_gif(tmp_path: Path) -> None:
    from src.features.edit.core.timeline_export import TimelineExporter
    from src.shared.export.gif_encoder import GifVideoEncoder
    from src.shared.export.video_encoder import FFmpegEncoderConfig
    from src.shared.media.metadata import Rational

    config = FFmpegEncoderConfig(
        codec="gif",
        width=32,
        height=18,
        fps=Rational(24, 1),
        pix_fmt="rgb24",
    )
    encoder = TimelineExporter._create_encoder(
        object(), str(tmp_path / "factory.gif"), config
    )
    assert isinstance(encoder, GifVideoEncoder)
