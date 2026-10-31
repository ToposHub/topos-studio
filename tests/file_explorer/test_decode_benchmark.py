"""Focused tests for the source decode benchmark's pure logic."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FILE_EXPLORER = ROOT / "external" / "file_explorer"
if str(FILE_EXPLORER) not in sys.path:
    sys.path.insert(0, str(FILE_EXPLORER))

from plugins.decode_benchmark.benchmark import (  # noqa: E402
    BenchmarkConfig,
    BenchmarkResult,
    DecodeBenchmarkRunner,
    codec_family,
    export_results_csv,
    export_results_json,
    percentile,
    resolution_bucket,
)


def test_codec_and_resolution_classification() -> None:
    assert codec_family("h264") == "H.264"
    assert codec_family("hevc") == "H.265 / HEVC"
    assert codec_family("prores_ks") == "ProRes"
    assert codec_family("dnxhr_hqx") == "DNxHR / DNxHD"
    assert codec_family("libaom-av1") == "AV1"
    assert codec_family("libvpx-vp9") == "VP9"
    assert codec_family("libvpx") == "VP8"
    assert codec_family("mpeg2video") == "MPEG-2"
    assert codec_family("mpeg4") == "MPEG-4"
    assert codec_family("mjpeg") == "MJPEG"
    assert codec_family("ffv1") == "FFV1"
    assert resolution_bucket(2048, 1080) == "2K"
    assert resolution_bucket(4096, 2160) == "4K"
    assert resolution_bucket(6144, 3456) == "6K"
    assert resolution_bucket(7680, 4320) == "8K"


def test_percentile_and_config_normalization() -> None:
    assert percentile([], 95) == 0.0
    assert percentile([1.0, 2.0, 4.0], 50) == 2.0
    assert percentile([1.0, 2.0, 4.0], 95) == 3.8
    config = BenchmarkConfig(frame_count=0, warmup_frames=-3, mode="other", pattern="other").normalized()  # type: ignore[arg-type]
    assert config.frame_count == 1
    assert config.warmup_frames == 0
    assert config.mode == "proxy"
    assert config.pattern == "sequential"


def test_frame_indices_are_warmup_aware_and_repeatable() -> None:
    config = BenchmarkConfig(frame_count=5, warmup_frames=3, pattern="sequential")
    assert DecodeBenchmarkRunner._frame_indices(20, config) == [3, 4, 5, 6, 7]
    random_config = BenchmarkConfig(frame_count=12, warmup_frames=2, pattern="random")
    indices = DecodeBenchmarkRunner._frame_indices(10, random_config)
    assert len(indices) == 8
    assert indices == DecodeBenchmarkRunner._frame_indices(10, random_config)
    assert len(set(indices)) == len(indices)


def test_result_exports_json_and_csv(tmp_path: Path) -> None:
    result = BenchmarkResult(
        path="/tmp/clip.mov",
        filename="clip.mov",
        codec="h264",
        codec_family="H.264",
        resolution="4K",
        width=3840,
        height=2160,
        fps=24.0,
        bit_depth=8,
        pixel_format="yuv420p",
        mode="proxy",
        pattern="sequential",
        requested_frames=10,
        frames_decoded=10,
        elapsed_ms=100.0,
        decode_fps=100.0,
        status="ok",
    )
    json_path = tmp_path / "result.json"
    csv_path = tmp_path / "result.csv"
    export_results_json(json_path, [result])
    export_results_csv(csv_path, [result])
    assert json.loads(json_path.read_text(encoding="utf-8"))[0]["codec"] == "h264"
    assert "decode_fps" in csv_path.read_text(encoding="utf-8-sig")
