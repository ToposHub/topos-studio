from __future__ import annotations

from fractions import Fraction

from src.shared.media.ffmpeg_source import FFmpegMediaSource
from src.shared.media.metadata import Rational, VideoMetadata


class _Stream:
    time_base = Fraction(1, 30000)


def _source_with_ntsc_timebase() -> FFmpegMediaSource:
    source = FFmpegMediaSource("/tmp/nonexistent.mov")
    source._video_stream = _Stream()
    source._metadata = VideoMetadata(
        width=1920,
        height=1080,
        fps=Rational(30000, 1001),
        total_frames=3000,
        codec_name="h264",
        codec_profile=None,
        pixel_format="yuv420p",
        bit_depth=8,
    )
    return source


def test_pts_to_frame_uses_rational_rounding_for_ntsc_rates() -> None:
    """29.97 fps PTS values should map back to nearest frame, not floor."""
    source = _source_with_ntsc_timebase()

    pts = source._frame_to_pts(10)

    assert pts == 10010
    assert source._pts_to_frame(pts) == 10


def test_pts_to_frame_rounds_nearby_timestamp_to_expected_frame() -> None:
    source = _source_with_ntsc_timebase()

    assert source._pts_to_frame(10009) == 10
