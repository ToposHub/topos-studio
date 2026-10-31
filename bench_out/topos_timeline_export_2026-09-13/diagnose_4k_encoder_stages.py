#!/usr/bin/env python3
"""Profile the real 4K TOPOS HQ encode path without changing product code.

The first N ProRes 422 HQ frames are decoded once and retained in memory. The
encoder then runs on those exact frames while wrappers independently time
planar validation/preparation, product native encode/rate control, MOV mux
packet insertion, and the mux's actual write callbacks. This is intended to be
run while the machine is otherwise idle because it is a throughput benchmark.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.shared.codec.color_metadata_config import ColorMetadataConfig
from src.shared.codec.topos_binding import ToposCodec, ToposMovieFile, ToposMuxFile
from src.shared.export.topos_encoder import ToposVideoEncoder
from src.shared.export.video_encoder import FFmpegEncoderConfig
from src.shared.media.ffmpeg_source import FFmpegMediaSource
from src.shared.media.metadata import Rational


SOURCE = ROOT / "tests" / "test_videos" / "4K_prores422HQ.mov"
OUT = Path(__file__).resolve().parent / "diagnostic_4k_components.mov"
RESULT = Path(__file__).resolve().parent / "diagnostic_4k_components.json"
FRAME_COUNT = max(1, int(os.environ.get("TOPOS_COMPONENT_FRAMES", "120")))
SLICE_THREADS = 16


def _stats(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "total_s": round(sum(values), 6),
        "mean_ms": round(statistics.mean(values) * 1000, 3) if values else 0.0,
        "median_ms": round(statistics.median(values) * 1000, 3) if values else 0.0,
        "p95_ms": round(sorted(values)[min(len(values) - 1, round(0.95 * (len(values) - 1)))] * 1000, 3) if values else 0.0,
        "equivalent_fps": round(len(values) / sum(values), 3) if values and sum(values) else 0.0,
    }


def _timed_method(owner: Any, method_name: str, bucket: list[float]) -> None:
    original: Callable[..., Any] = getattr(owner, method_name)

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            bucket.append(time.perf_counter() - started)

    setattr(owner, method_name, wrapped)


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    if not SOURCE.is_file():
        raise FileNotFoundError(SOURCE)
    if OUT.exists() or RESULT.exists():
        raise FileExistsError(f"Refusing to overwrite diagnostic output: {OUT} or {RESULT}")

    source = FFmpegMediaSource(
        str(SOURCE), preserve_planes=True, output_mode="planar_yuv")
    decode_times: list[float] = []
    frames = []
    source.open()
    try:
        stream = source._video_stream
        decoder_threads = int(stream.codec_context.thread_count)
        for index in range(FRAME_COUNT):
            started = time.perf_counter()
            frame = source.read_next_frame()
            decode_times.append(time.perf_counter() - started)
            if frame.pixel_format != "yuv422p10le" or not frame.planes:
                raise RuntimeError(
                    f"Expected planar yuv422p10le, got {frame.pixel_format!r}")
            frames.append(frame)
    finally:
        source.close()

    first = frames[0]
    if (first.width, first.height) != (3840, 2160):
        raise RuntimeError(f"Expected 3840x2160 input, got {first.width}x{first.height}")

    config = FFmpegEncoderConfig(
        codec="topos",
        width=first.width,
        height=first.height,
        fps=Rational(25, 1),
        pix_fmt="yuv422p10le",
        profile="hq",
        color_metadata=ColorMetadataConfig.sdr(),
        has_alpha=False,
        crf=None,
        entropy_mode="rans2",
        aq_mode="off",
        rdo_mode="off",
    )

    metric: dict[str, Any] = {
        "prepare_planar_s": [],
        "product_encode_packet_s": [],
        "mux_add_packet_s": [],
        "encode_frame_s": [],
        "mux_write_callback_s": [],
        "mux_seek_write_callback_s": [],
    }
    callback_bytes = {"write": 0, "seek_write": 0}
    original_write = ToposMuxFile._write_cb
    original_seek_write = ToposMuxFile._seek_write_cb

    def measured_write(self: ToposMuxFile, ctx: int, data: int, length: int) -> int:
        started = time.perf_counter()
        try:
            return original_write(self, ctx, data, length)
        finally:
            metric["mux_write_callback_s"].append(time.perf_counter() - started)
            callback_bytes["write"] += int(length)

    def measured_seek_write(
        self: ToposMuxFile, ctx: int, off: int, data: int, length: int,
    ) -> int:
        started = time.perf_counter()
        try:
            return original_seek_write(self, ctx, off, data, length)
        finally:
            metric["mux_seek_write_callback_s"].append(time.perf_counter() - started)
            callback_bytes["seek_write"] += int(length)

    ToposMuxFile._write_cb = measured_write  # type: ignore[method-assign]
    ToposMuxFile._seek_write_cb = measured_seek_write  # type: ignore[method-assign]
    encoder = ToposVideoEncoder(
        str(OUT), config, slice_threads=SLICE_THREADS, slice_rows=16)
    try:
        encoder.open()
        _timed_method(encoder, "_prepare_planes", metric["prepare_planar_s"])
        _timed_method(encoder, "_encode_packet", metric["product_encode_packet_s"])
        _timed_method(encoder._mux, "add_packet", metric["mux_add_packet_s"])
        _timed_method(encoder, "encode_frame", metric["encode_frame_s"])
        encode_started = time.perf_counter()
        for frame in frames:
            encoder.encode_frame(frame)
        encode_loop_s = time.perf_counter() - encode_started
        close_started = time.perf_counter()
        encoder.close()
        close_s = time.perf_counter() - close_started
    except BaseException:
        encoder.abort()
        raise
    finally:
        ToposMuxFile._write_cb = original_write  # type: ignore[method-assign]
        ToposMuxFile._seek_write_cb = original_seek_write  # type: ignore[method-assign]

    codec = ToposCodec()
    movie = ToposMovieFile(codec, str(OUT))
    try:
        sample_count = int(movie.info().sample_count)
    finally:
        movie.close()
    if sample_count != FRAME_COUNT:
        raise RuntimeError(
            f"Output has {sample_count} frames, expected {FRAME_COUNT}")

    output_bytes = OUT.stat().st_size
    output_duration = FRAME_COUNT / 25.0
    prepare_s = sum(metric["prepare_planar_s"])
    packet_s = sum(metric["product_encode_packet_s"])
    mux_s = sum(metric["mux_add_packet_s"])
    frame_wrapper_s = sum(metric["encode_frame_s"])
    write_callback_s = sum(metric["mux_write_callback_s"])
    seek_callback_s = sum(metric["mux_seek_write_callback_s"])
    summary = {
        "protocol": "same-decoded-frames-component-timing",
        "source": str(SOURCE),
        "source_codec": "ProRes 422 HQ",
        "resolution": [first.width, first.height],
        "pixel_format": first.pixel_format,
        "frames": FRAME_COUNT,
        "fps": 25.0,
        "slice_threads": SLICE_THREADS,
        "slice_rows": 16,
        "decoder_threads": decoder_threads,
        "decode_sequential": _stats(decode_times),
        "encode_loop_s": round(encode_loop_s, 6),
        "encode_loop_fps": round(FRAME_COUNT / encode_loop_s, 3),
        "close_finalize_s": round(close_s, 6),
        "stage_timings": {
            key.removesuffix("_s"): _stats(value)
            for key, value in metric.items()
        },
        "derived_stage_totals": {
            "prepare_plus_native_encode_plus_mux_s": round(
                prepare_s + packet_s + mux_s, 6),
            "encode_frame_internal_overhead_ms_per_frame": round(
                max(0.0, frame_wrapper_s - prepare_s - packet_s - mux_s)
                / FRAME_COUNT * 1000, 3),
            "mux_callback_time_ms_per_frame": round(
                (write_callback_s + seek_callback_s) / FRAME_COUNT * 1000, 3),
            "mux_add_packet_noncallback_time_ms_per_frame": round(
                max(0.0, mux_s - write_callback_s - seek_callback_s)
                / FRAME_COUNT * 1000, 3),
            "harness_loop_overhead_ms_per_frame": round(
                max(0.0, encode_loop_s - frame_wrapper_s)
                / FRAME_COUNT * 1000, 3),
        },
        "mux_callback_bytes": callback_bytes,
        "output_path": str(OUT),
        "output_size_bytes": output_bytes,
        "output_sample_count": sample_count,
        "output_bitrate_mbps_at_25fps": round(
            output_bytes * 8 / output_duration / 1_000_000, 3),
        "comparison_reference": {
            "standalone_native_4k_hq_fps": 53.212,
            "previous_full_timeline_stable_fps": 33.745,
            "previous_timeline_encode_frame_mean_ms": 29.917,
            "scope_note": (
                "Decode is completed before encoding to isolate the encoder; the separate "
                "decode measurement is not added to encode_loop_fps. Native-only baseline "
                "excludes mux and Python frame preparation."
            ),
        },
    }
    RESULT.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
