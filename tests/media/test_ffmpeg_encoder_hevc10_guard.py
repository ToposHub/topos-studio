"""The public wrapper keeps software Main 10 out of the caller process."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from src.shared.export.video_encoder import FFmpegEncoderConfig, FFmpegVideoEncoder
from src.shared.media.metadata import Rational


def _config(codec: str, pix_fmt: str, hwaccel: str = "off") -> FFmpegEncoderConfig:
    return FFmpegEncoderConfig(
        codec=codec, width=320, height=180, fps=Rational(25, 1),
        pix_fmt=pix_fmt, hwaccel=hwaccel,
    )


@pytest.mark.parametrize("pixel_format", ["yuv420p10le", "p010le"])
def test_software_hevc_10bit_opens_only_in_worker(tmp_path, pixel_format) -> None:
    output = str(tmp_path / "master.mov")
    config = _config("hevc", pixel_format)
    with patch("av.open") as local_open, patch(
        "src.shared.export.hdr_hevc_encoder.HdrHevcEncoder",
    ) as worker:
        encoder = FFmpegVideoEncoder(output, config)
        encoder.open()
        local_open.assert_not_called()
        assert worker.call_args.args == (output, config)
        worker.return_value.open.assert_called_once()
        encoder.abort()
        worker.return_value.abort.assert_called_once()


@pytest.mark.parametrize("stage", ["probe", "session"])
def test_failed_hardware_main10_rebuild_uses_worker(tmp_path, stage) -> None:
    config = _config("hevc", "yuv420p10le", "nvenc")
    container = Mock()
    container.add_stream.return_value.codec_context.open.side_effect = RuntimeError("test driver refusal")
    with (
        patch.object(FFmpegEncoderConfig, "hwaccel_engaged", return_value=True),
        patch.object(FFmpegVideoEncoder, "_probe_encoder_open", return_value=stage == "session"),
        patch.object(FFmpegVideoEncoder, "_configure_encoder"),
        patch.object(FFmpegVideoEncoder, "_set_color_metadata"),
        patch("av.open", return_value=container),
        patch("src.shared.export.hdr_hevc_encoder.HdrHevcEncoder") as worker,
    ):
        encoder = FFmpegVideoEncoder(str(tmp_path / "fallback.mov"), config)
        encoder.open()
        if stage == "session":
            container.add_stream.assert_called_once()
            container.close.assert_called_once()
        else:
            container.add_stream.assert_not_called()
        worker.return_value.open.assert_called_once()
        assert encoder.hw_fallback_reason.startswith("encoder_hw_unavailable:nvenc")
        assert encoder.hw_encoder_name == ""
        encoder.abort()


@pytest.mark.parametrize(("codec", "pixel_format", "native_name"), [
    ("hevc", "yuv420p", "libx265"),
    ("h264", "yuv420p10le", "libx264"),
])
def test_other_formats_keep_the_regular_encoder(tmp_path, codec, pixel_format, native_name) -> None:
    with patch("av.open") as local_open, patch(
        "src.shared.export.hdr_hevc_encoder.HdrHevcEncoder",
    ) as worker:
        encoder = FFmpegVideoEncoder(str(tmp_path / "regular.mov"), _config(codec, pixel_format))
        encoder.open()
        assert local_open.return_value.add_stream.call_args.args == (native_name,)
        worker.assert_not_called()
        encoder.abort()
