"""Real-material media-probe matrix for the checked-in editorial assets.

Uses the production backend selection (``create_media_probe``: PyAV
in-process first, ffprobe CLI fallback) so the matrix exercises the same
path as packaged builds instead of hard-wiring the CLI-only backend.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.real_media

from src.features.edit.ai_editing.delivery_analyzer import FfmpegDeliveryAnalyzer
from src.features.edit.ai_editing.media_probe import create_media_probe


_VIDEO_DIR = Path(__file__).parents[2] / "test_videos"
_CASES = (
    ("2K_DNxHR HQX 12-bit.mov", (1920, 1080)),
    ("2K_H265.mov", (1920, 1080)),
    ("2K_h264.mp4", (1920, 1080)),
    ("2K_prores422HQ.mov", (1920, 1080)),
    ("4K_DNxHR HQX 12-bit.mov", (3840, 2160)),
    ("4K_H264.mov", (3840, 2160)),
    ("4K_H265.mov", (3840, 2160)),
    ("4K_prores422HQ.mov", (3840, 2160)),
    ("4K_prores4444.mov", (3840, 2160)),
)


@pytest.mark.parametrize("filename, expected_resolution", _CASES)
def test_real_video_probe_matrix(filename: str, expected_resolution: tuple[int, int]) -> None:
    path = _VIDEO_DIR / filename
    if not path.is_file():
        pytest.skip(f"real test asset is not available: {filename}")

    # The matrix validates real containers/codecs without hashing multi-GB
    # mezzanine files; production export completion keeps the default hash.
    result = create_media_probe(include_output_hash=False).probe(path)

    assert result.succeeded, (filename, result.error_code)
    assert result.quality_input.output_exists is True
    assert result.quality_input.output_reopenable is True
    assert result.quality_input.resolution == expected_resolution
    assert result.quality_input.duration_seconds is not None
    assert result.metadata is not None
    assert result.metadata["video_codec"]
    assert result.metadata["pixel_format"]
    assert result.metadata["container"]
    assert result.metadata["file_size_bytes"] > 0
    assert result.quality_input.video_codec
    assert result.quality_input.container
    assert result.quality_input.file_size_bytes > 0
    assert result.quality_input.video_bitrate_kbps is not None


def test_real_h264_delivery_analysis() -> None:
    path = _VIDEO_DIR / "2K_h264.mp4"
    if not path.is_file():
        pytest.skip("real H.264 test asset is not available")

    probe_result = create_media_probe(include_output_hash=False).probe(path)
    analysis = FfmpegDeliveryAnalyzer(sample_fps=1.0, timeout_seconds=120.0).analyze(
        path,
        audio_present=probe_result.quality_input.audio_present,
        audio_sample_rate=probe_result.quality_input.audio_sample_rate,
    )

    assert probe_result.succeeded
    assert analysis.succeeded
    assert analysis.sampled_frame_count > 0
    assert analysis.black_frame_count == 0
    assert analysis.frozen_frame_count == 0
    assert analysis.audio_present is True
    assert analysis.peak_db is not None
    assert analysis.integrated_lufs is not None
    assert analysis.loudness_backend_available is True
