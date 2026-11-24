"""The public wrapper keeps software Main 10 out of the caller process."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from pathlib import Path
import sys
from unittest.mock import Mock, patch

import numpy as np
import pytest

from src.shared.export.video_encoder import FFmpegEncoderConfig, FFmpegVideoEncoder
from src.shared.media.frame import DecodedFrame
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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Main 10 FFmpeg route")
def test_video_only_main10_without_ffmpeg_keeps_isolated_worker(tmp_path: Path) -> None:
    config = replace(_config("hevc", "yuv420p10le"), video_only=True)
    with (
        patch("src.shared.ffmpeg_locator.find_ffmpeg", return_value=None),
        patch("src.shared.export.hdr_hevc_encoder.HdrHevcEncoder") as worker,
    ):
        encoder = FFmpegVideoEncoder(str(tmp_path / "fallback.mp4"), config, total_frames=4)
        encoder.open()
        worker.return_value.open.assert_called_once()
        encoder.abort()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Main 10 FFmpeg route")
@pytest.mark.parametrize("exit_during_encode", [False, True])
def test_windows_simd_main10_delivery(
    tmp_path: Path, exit_during_encode: bool,
) -> None:
    import av

    from src.shared.export.windows_main10_encoder import WindowsMain10Encoder

    config = replace(
        _config("hevc", "yuv420p10le"), width=66, height=66, fps=Rational(24000, 1001),
        video_only=True, crf=18, preset="ultrafast",
    )
    output = tmp_path / "old master.mov"
    old = b"previous export"
    output.write_bytes(old)
    encoder = FFmpegVideoEncoder(str(output), config, total_frames=4)
    encoder.open()
    try:
        isolated = encoder._main10_encoder
        if not isinstance(isolated, WindowsMain10Encoder):
            pytest.skip("Installed FFmpeg lacks SIMD Main 10 encoding")
        for index in range(4):
            pixels = np.full((66, 66, 3), 60 + index * 32, dtype=np.uint8)
            encoder.encode_frame(DecodedFrame(
                frame_index=index, pts=index, time_seconds=index * 1001 / 24000,
                data=pixels, pixel_format="bgr24", width=66, height=66,
            ))
        if exit_during_encode:
            process = isolated._process
            assert process is not None
            process.kill()
            process.wait(timeout=5)
            with pytest.raises(RuntimeError, match="Main 10 FFmpeg exited"):
                encoder.close(expected_frames=4)
            assert output.read_bytes() == old
        else:
            assert encoder.close(should_cancel=lambda: False, expected_frames=4)
            with av.open(str(output)) as movie:
                stream = movie.streams.video[0]
                assert stream.codec_context.profile == "Main 10"
                assert stream.codec_context.codec_tag == "hvc1"
                frames = list(movie.decode(video=0))
                assert [frame.pts * frame.time_base for frame in frames] == [
                    Fraction(index * 1001, 24000) for index in range(4)
                ]
                assert all(frame.format.name == "yuv420p10le" for frame in frames)
    finally:
        encoder.abort()
    assert not list(tmp_path.glob(".topos-hdr-*"))
