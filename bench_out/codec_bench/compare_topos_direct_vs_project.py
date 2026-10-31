#!/usr/bin/env python3
"""Benchmark the native Topos codec against the real timeline export.

The benchmark deliberately separates codec work from timeline work:

* direct decode: Topos MOV packet I/O + ``ToposDecoder.decode_views`` into
  caller-owned reusable uint16 planes;
* direct encode: ``ToposCodec.encode_frame`` on a real decoded Topos frame;
* application encode: ``ToposVideoEncoder`` with planar input and fixed QP,
  which exposes Python validation/mux overhead without RGB conversion;
* project export: metrics captured by ``run_real_h264_ab.py``.

The direct encode input should be the freshly exported project MOV so that its
resolution, pixel format, entropy mode, and representative content match the
application output.  No synthetic frames are used.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import statistics
import time
from pathlib import Path
from typing import Any

import numpy as np

from src.shared.codec.topos_binding import (
    TOPOS_CODEC_ABI_VERSION,
    ToposCodec,
    ToposDecoder,
    ToposMovieFile,
    _CFrameConfig,
)


def _decode_reusable(
    codec: ToposCodec,
    movie: ToposMovieFile,
    decoder: ToposDecoder,
    index: int,
    planes: list[np.ndarray] | None = None,
    views: Any = None,
) -> tuple[list[np.ndarray], Any, Any]:
    packet, size = movie.packet_into(index)
    prepared = decoder.prepare(packet, size)
    if planes is None:
        planes = []
        for plane in range(prepared.plane_count):
            width, height = codec.plane_geometry(prepared, plane)
            planes.append(np.empty((height, width), dtype="<u2"))
        views = decoder.make_views(planes)
    info = decoder.decode_views(packet, views, prepared.plane_count, size)
    return planes, views, info


def benchmark_decode(source: Path, frames: int, workers: int) -> dict[str, Any]:
    codec = ToposCodec()
    with ToposMovieFile(codec, str(source)) as movie:
        summary = movie.info()
        count = min(int(frames), int(summary.sample_count))
        if count < 1:
            raise ValueError("source has no samples")
        with ToposDecoder(codec, max_slice_workers=int(workers)) as decoder:
            planes, views, _ = _decode_reusable(codec, movie, decoder, 0)
            warmup = min(8, max(0, count - 1))
            for index in range(1, warmup + 1):
                _decode_reusable(codec, movie, decoder, index, planes, views)
            timed_start = warmup + 1
            timed_count = count - timed_start
            if timed_count <= 0:
                timed_start, timed_count = 0, count
            codec.decode_stage_stats_reset()
            started = time.perf_counter()
            checksum = 0
            for index in range(timed_start, timed_start + timed_count):
                _decode_reusable(codec, movie, decoder, index, planes, views)
                # A tiny scalar read keeps a real output dependency without
                # scanning the decoded planes or polluting the hot path.
                checksum ^= int(planes[0][index % planes[0].shape[0], index % planes[0].shape[1]])
            elapsed = time.perf_counter() - started
            stage = codec.decode_stage_stats_get()
    return {
        "source": str(source),
        "width": int(summary.width),
        "height": int(summary.height),
        "pixel_format": int(summary.pixel_format),
        "bit_depth": int(summary.bit_depth),
        "sample_count": int(summary.sample_count),
        "frames": int(timed_count),
        "warmup_frames": int(warmup),
        "wall_s": elapsed,
        "fps": timed_count / elapsed,
        "ms_per_frame": elapsed * 1000.0 / timed_count,
        "checksum": checksum,
        "stage_stats": stage.__dict__,
        "planes": [[int(p.shape[1]), int(p.shape[0])] for p in planes],
        "threads": int(workers),
    }


def _config_from_summary(summary: Any, packet: bytes, qp: int | None = None) -> _CFrameConfig:
    cfg = _CFrameConfig()
    cfg.struct_size = ctypes.sizeof(_CFrameConfig)
    cfg.abi_version = TOPOS_CODEC_ABI_VERSION
    values = {
        "visible_width": int(summary.width),
        "visible_height": int(summary.height),
        "profile": int(summary.profile),
        "pixel_format": int(summary.pixel_format),
        "bit_depth": int(summary.bit_depth),
        "qmatrix_id": int(summary.qmatrix_id),
        "qp_base": int(packet[29] if qp is None else qp),
        "qp_delta_luma": int(summary.qp_delta_luma),
        "qp_delta_chroma": int(summary.qp_delta_chroma),
        "slice_rows": 16,
        "alpha_mode": int(summary.alpha_mode),
        "alpha_bit_depth": int(summary.alpha_bit_depth),
        "alpha_premultiplied": int(summary.alpha_premultiplied),
        "color_range": int(summary.color_range),
        "color_primaries": int(summary.color_primaries),
        "color_transfer": int(summary.color_transfer),
        "color_matrix": int(summary.color_matrix),
        "chroma_siting": int(summary.chroma_siting),
        "sar_num": int(summary.sar_num),
        "sar_den": int(summary.sar_den),
    }
    for name, value in values.items():
        setattr(cfg, name, value)
    # V7-R2 is encoded as native reserved[0]=8 and frame header byte 45=7.
    # Keep the source stream's entropy generation when it is visible.
    cfg.reserved[0] = 8 if len(packet) > 48 and packet[45] == 7 else 0
    return cfg


def _load_one_frame(path: Path) -> tuple[ToposCodec, Any, list[np.ndarray], bytes]:
    codec = ToposCodec()
    movie = ToposMovieFile(codec, str(path))
    summary = movie.info()
    packet, size = movie.packet_into(0)
    packet_bytes = ctypes.string_at(packet, size)
    with ToposDecoder(codec, max_slice_workers=16) as decoder:
        prepared = decoder.prepare(packet, size)
        planes: list[np.ndarray] = []
        for plane in range(prepared.plane_count):
            width, height = codec.plane_geometry(prepared, plane)
            planes.append(np.empty((height, width), dtype="<u2"))
        decoder.decode_views(packet, decoder.make_views(planes), prepared.plane_count, size)
    movie.close()
    return codec, summary, planes, packet_bytes


def benchmark_encode(source: Path, frames: int, workers: int, target_bytes: int | None) -> dict[str, Any]:
    codec, summary, planes, packet = _load_one_frame(source)
    cfg = _config_from_summary(summary, packet)
    codec.set_slice_threads(workers)
    for _ in range(8):
        codec.encode_frame(cfg, planes)
    codec.encode_stage_stats_reset()
    started = time.perf_counter()
    sizes: list[int] = []
    for _ in range(int(frames)):
        out, _stats = codec.encode_frame(cfg, planes)
        sizes.append(len(out))
    elapsed = time.perf_counter() - started
    stage = codec.encode_stage_stats_get()
    result: dict[str, Any] = {
        "source": str(source),
        "width": int(summary.width),
        "height": int(summary.height),
        "frames": int(frames),
        "qp": int(cfg.qp_base),
        "entropy_reserved0": int(cfg.reserved[0]),
        "wall_s": elapsed,
        "fps": int(frames) / elapsed,
        "ms_per_frame": elapsed * 1000.0 / int(frames),
        "packet_bytes_mean": statistics.mean(sizes),
        "packet_bytes_min": min(sizes),
        "packet_bytes_max": max(sizes),
        "stage_stats": stage.__dict__,
    }
    if target_bytes is not None:
        # One short run measures rate-control search separately.  The project
        # does this only on the first frame, then uses feedback single-pass.
        for _ in range(2):
            codec.encode_sized(cfg, planes, int(target_bytes), qp_min=0, qp_max=95)
        sized_n = min(8, int(frames))
        started = time.perf_counter()
        sized_qps: list[int] = []
        sized_sizes: list[int] = []
        for _ in range(sized_n):
            out, stats, used = codec.encode_sized(cfg, planes, int(target_bytes), qp_min=0, qp_max=95)
            sized_qps.append(int(used))
            sized_sizes.append(len(out))
        sized_elapsed = time.perf_counter() - started
        result["sized_probe"] = {
            "target_bytes": int(target_bytes),
            "frames": sized_n,
            "wall_s": sized_elapsed,
            "fps": sized_n / sized_elapsed,
            "qp_values": sized_qps,
            "packet_bytes": sized_sizes,
        }
    return result


def benchmark_application_encoder(source: Path, frames: int, workers: int) -> dict[str, Any]:
    from src.shared.export.topos_encoder import ToposVideoEncoder
    from src.shared.export.video_encoder import FFmpegEncoderConfig
    from src.shared.media.frame import DecodedFrame
    from src.shared.media.metadata import Rational

    codec, summary, planes, packet = _load_one_frame(source)
    qp = int(packet[29])
    frame = DecodedFrame(
        frame_index=0,
        pts=0,
        time_seconds=0.0,
        planes=tuple(planes),
        pixel_format="yuv422p10le",
        width=int(summary.width),
        height=int(summary.height),
    )
    cfg = FFmpegEncoderConfig(
        codec="topos", width=int(summary.width), height=int(summary.height),
        fps=Rational(25, 1), pix_fmt="yuv422p10le", profile="proxy", crf=qp,
    )
    out = Path("/tmp/topos_direct_application_encoder_bench.mov")
    encoder = ToposVideoEncoder(str(out), cfg, slice_threads=workers, slice_rows=16)
    encoder.open()
    for _ in range(4):
        encoder.encode_frame(frame)
    started = time.perf_counter()
    for index in range(int(frames)):
        frame.frame_index = index
        frame.pts = index
        frame.time_seconds = index / 25.0
        encoder.encode_frame(frame)
    encoder.close()
    elapsed = time.perf_counter() - started
    return {
        "source": str(source),
        "frames": int(frames),
        "qp": qp,
        "wall_s": elapsed,
        "fps": int(frames) / elapsed,
        "ms_per_frame": elapsed * 1000.0 / int(frames),
        "output_bytes": out.stat().st_size,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--encode-source", type=Path, required=True)
    parser.add_argument("--project-json", type=Path, required=True)
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--threads", type=int, default=16)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    project = json.loads(args.project_json.read_text())
    direct_decode = benchmark_decode(args.source, args.frames, args.threads)
    target_bytes = None
    encoded_frames = int(project.get("frames_encoded_reported") or project.get("decoded_frame_calls") or 0)
    if encoded_frames:
        target_bytes = max(1, int(round(int(project.get("output_bytes", 0)) / encoded_frames)))
    direct_encode = benchmark_encode(args.encode_source, args.frames, args.threads, target_bytes)
    app_encode = benchmark_application_encoder(args.encode_source, args.frames, args.threads)
    project_decode_fps = float(project["decoded_frame_calls"]) / float(project["decode_wall_s"])
    project_encode_fps = float(project["frames_encoded_reported"]) / float(project["encode_frame_wall_s"])
    result = {
        "schema": "topos.codec_vs_project.v1",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": {"platform": os.uname().sysname, "machine": os.uname().machine},
        "codec_version": str(ToposCodec().version()),
        "direct_decode": direct_decode,
        "direct_encode": direct_encode,
        "application_encoder_fixed_qp": app_encode,
        "project_export": {
            "metrics_file": str(args.project_json),
            "frames": int(project["frames_encoded_reported"]),
            "decode_wall_s": float(project["decode_wall_s"]),
            "decode_fps": project_decode_fps,
            "gpu_render_wall_s": float(project["gpu_render_wall_s"]),
            "gpu_render_fps": float(project["gpu_renderers"][0]["frames_rendered"]) / float(project["gpu_render_wall_s"]),
            "encode_frame_wall_s": float(project["encode_frame_wall_s"]),
            "encode_fps": project_encode_fps,
            "export_wall_s": float(project["export_wall_s"]),
            "export_fps": float(project["export_fps"]),
            "output_bytes": int(project["output_bytes"]),
        },
        "comparison": {
            "direct_decode_vs_project_decode": direct_decode["fps"] / project_decode_fps,
            "direct_encode_vs_project_encode": direct_encode["fps"] / project_encode_fps,
            "application_fixed_qp_vs_project_encode": app_encode["fps"] / project_encode_fps,
            "project_export_over_direct_encode": float(project["export_fps"]) / direct_encode["fps"],
        },
        "notes": [
            "Direct decode includes native Topos MOV packet I/O and decode_views into reusable caller-owned planes; it excludes timeline effects and Metal composition.",
            "Direct encode uses one real decoded frame at the project's output resolution and the packet's representative per-frame QP; no synthetic content is used.",
            "Application fixed-QP encode includes ToposVideoEncoder validation and MOV mux overhead; project export uses rate-control feedback and varying QP, so its encode stage is not a fixed-QP apples-to-apples run.",
            "Project export includes decode, Metal render, frame scheduling, encode, mux, and finalization; export_fps is the end-to-end number.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
