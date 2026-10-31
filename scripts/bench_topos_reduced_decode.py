#!/usr/bin/env python3
"""Benchmark Topos full/post-scale/reduced decode on one identical packet.

For a V7-B packet, ``v7_base`` executes the real AUTO_2K base-only request;
for a legacy packet it remains an explicit unsupported diagnostic row. V7-A
band mode is kept as a future row until a public packet source is available.

Examples::

    python3 scripts/bench_topos_reduced_decode.py --packet frame.toos \
        --scale 3 --iterations 20 --warmup 5 --output reduced.json
    python3 scripts/bench_topos_reduced_decode.py --source corpus.png \
        --scale 3 --threads 4
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SCALE_BY_DIVISOR = {2: 1, 3: 2, 4: 3, 8: 4}
MODE_NAMES = ("full", "post_scale", "legacy_reduced", "v7_band", "v7_base")


def _percentiles(samples_ns: list[int]) -> dict[str, float]:
    ordered = sorted(samples_ns)
    if not ordered:
        return {key: 0.0 for key in ("p50", "p95", "p99", "max", "mean")}

    def percentile(percent: int) -> float:
        return ordered[((len(ordered) - 1) * percent) // 100] / 1_000_000.0

    return {
        "p50": percentile(50),
        "p95": percentile(95),
        "p99": percentile(99),
        "max": ordered[-1] / 1_000_000.0,
        "mean": statistics.fmean(ordered) / 1_000_000.0,
    }


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _plane_shape(info: Any, plane: int) -> tuple[int, int]:
    width = int(info.visible_width)
    if int(info.pixel_format) == 0 and plane in (1, 2):
        width = (width + 1) // 2
    return int(info.visible_height), width


def _is_v7b_packet(packet: bytes) -> bool:
    """Check the cheap V7-B dispatch signature before full output allocation."""
    return (
        len(packet) >= 61 and packet[:4] == b"TPIC" and packet[6] == 7 and
        packet[53:57] == b"TPLD" and int.from_bytes(packet[59:61], "big") == 1
    )


def _v7b_source_geometry(packet: bytes) -> tuple[int, int]:
    if not _is_v7b_packet(packet):
        raise ValueError("packet is not V7-B")
    return int.from_bytes(packet[23:25], "big"), int.from_bytes(packet[25:27], "big")


def _post_scale(frame: Any, divisor: int) -> int:
    """Nearest preview resize used only by the post-scale baseline."""
    import numpy as np

    source_width = int(frame.info.visible_width)
    source_height = int(frame.info.visible_height)
    target_width = (source_width + divisor - 1) // divisor
    target_height = (source_height + divisor - 1) // divisor
    output_bytes = 0
    for plane, data in enumerate(frame.planes):
        src_h, src_w = _plane_shape(frame.info, plane)
        arr = np.frombuffer(data, dtype="<u2", count=src_h * src_w).reshape(src_h, src_w)
        plane_width = target_width
        if int(frame.info.pixel_format) == 0 and plane in (1, 2):
            plane_width = (target_width + 1) // 2
        y = np.minimum((np.arange(target_height) * src_h) // target_height, src_h - 1)
        x = np.minimum((np.arange(plane_width) * src_w) // max(1, plane_width), src_w - 1)
        resized = arr[np.ix_(y, x)]
        output_bytes += int(resized.nbytes)
    return output_bytes


def _decode_once(codec: Any, packet: bytes, mode: str, divisor: int,
                 request: Any = None) -> int:
    scale = SCALE_BY_DIVISOR[divisor]
    if mode == "full":
        frame = codec.decode(packet)
        return sum(len(plane) for plane in frame.planes)
    if mode == "post_scale":
        return _post_scale(codec.decode(packet), divisor)
    if mode == "legacy_reduced":
        frame = codec.decode_reduced(packet, scale)
        return sum(len(plane) for plane in frame.planes)
    if mode == "v7_base":
        frame = codec.decode_request(packet, request)
        return sum(len(plane) for plane in frame.planes)
    raise ValueError(f"unsupported benchmark mode: {mode}")


def _telemetry(codec: Any) -> dict[str, int]:
    stats = codec.decode_stage_stats_get()
    return {key: int(value) for key, value in asdict(stats).items()}


def _run_state(codec: Any, packet: bytes, mode: str, divisor: int,
               target_size: tuple[int, int], cache_state: str,
               iterations: int, warmup: int, is_v7b: bool) -> dict[str, Any]:
    if mode == "v7_band" or (mode == "v7_base" and not is_v7b):
        return {
            "status": "unsupported",
            "reason": (
                "V7-B packet required for v7_base; V7-A public packet source "
                "is not available for v7_band"
            ),
            "iterations": 0,
        }

    request = None
    if mode == "v7_base":
        from src.shared.codec.topos_binding import TC_DECODE_MODE_AUTO_2K
        # Keep the comparison at the requested 2K geometry.  AUTO_2K still
        # selects the bounded V7-B base range; target_size only controls the
        # final base resampling (e.g. 4K/8K -> 1920x1080).
        request = codec.make_decode_request(
            TC_DECODE_MODE_AUTO_2K, target_size=target_size)
    for _ in range(warmup):
        _decode_once(codec, packet, mode, divisor, request)
    # Warmups are intentionally excluded from both the latency and telemetry
    # sample.  The native profile counters are cumulative, so reset after the
    # warmup phase rather than subtracting two concurrently changing structs.
    codec.decode_stage_stats_reset()
    samples_ns: list[int] = []
    output_bytes = 0
    for _ in range(iterations):
        start = time.perf_counter_ns()
        output_bytes = _decode_once(codec, packet, mode, divisor, request)
        samples_ns.append(time.perf_counter_ns() - start)
    return {
        "status": "ok",
        "cache_state": cache_state,
        "iterations": iterations,
        "latency_ms": _percentiles(samples_ns),
        "output_bytes": output_bytes,
        "telemetry": _telemetry(codec),
    }


def _source_packet(codec: Any, source: Path, scalable: bool,
                   base_max_dim: int) -> tuple[bytes, dict[str, Any]]:
    """Encode a PNG/TIFF source into one deterministic V1 or V7-B packet."""
    import cv2
    import numpy as np
    from src.shared.codec.topos_binding import (
        CVT_BGR24, CVT_BGR48, CVT_BGRA32, CVT_BGRA64, CVT_OUT_YUV422,
        _CFrameConfig,
    )

    image = cv2.imread(str(source), cv2.IMREAD_UNCHANGED)
    if image is None or image.ndim != 3 or image.shape[2] not in (3, 4):
        raise RuntimeError(f"source must be a BGR/BGRA 8/16-bit image: {source}")
    height, width, channels = image.shape
    if width > 0xFFFF or height > 0xFFFF:
        raise RuntimeError("source dimensions exceed the current frame ABI")
    image = np.ascontiguousarray(image)
    if image.dtype == np.uint8:
        fmt = CVT_BGRA32 if channels == 4 else CVT_BGR24
    elif image.dtype == np.uint16:
        fmt = CVT_BGRA64 if channels == 4 else CVT_BGR48
    else:
        raise RuntimeError(f"unsupported source dtype: {image.dtype}")
    planes = codec.convert_packed_rgb(
        image.reshape(height, width * channels), width, height,
        fmt=fmt, out_mode=CVT_OUT_YUV422, bit_depth=10,
        full_range=True, max_workers=1,
    )
    cfg = _CFrameConfig()
    cfg.struct_size = __import__("ctypes").sizeof(_CFrameConfig)
    cfg.abi_version = 2
    cfg.visible_width = width
    cfg.visible_height = height
    cfg.profile = 3
    cfg.pixel_format = 0
    cfg.bit_depth = 10
    cfg.qmatrix_id = 1
    cfg.qp_base = 24
    cfg.slice_rows = 16
    cfg.alpha_mode = 1 if channels == 4 else 0
    cfg.alpha_bit_depth = 16 if channels == 4 else 0
    cfg.color_range = 1
    cfg.color_primaries = 1
    cfg.color_transfer = 1
    cfg.color_matrix = 1
    cfg.sar_num = 1
    cfg.sar_den = 1
    if scalable:
        packet = codec.encode_scalable(cfg, planes, base_max_dim=base_max_dim)
        input_kind = "image_encoded_to_topos_v7b"
    else:
        packet, _stats = codec.encode_frame(cfg, planes)
        input_kind = "image_encoded_to_topos_v1"
    return packet, {
        "path": str(source),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "source_bytes": source.stat().st_size,
        "width": width,
        "height": height,
        "input_kind": input_kind,
    }


def _packet_source(packet_path: Path) -> tuple[bytes, dict[str, Any]]:
    packet = packet_path.read_bytes()
    return packet, {
        "path": str(packet_path),
        "source_sha256": hashlib.sha256(packet).hexdigest(),
        "source_bytes": len(packet),
        "input_kind": "elementary_frame_packet",
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    os.environ.setdefault("TOPOS_CODEC_PROFILE", "1")
    if args.threads is not None:
        os.environ["TOPOS_SLICE_THREADS"] = str(args.threads)
    from src.shared.codec import ToposCodec

    codec = ToposCodec()
    if args.packet is not None:
        packet, source = _packet_source(args.packet)
    else:
        packet, source = _source_packet(
            codec, args.source, bool(args.scalable), int(args.base_max_dim)
        )
    # One warm decode establishes geometry and validates the packet before the
    # timed states.  It is deliberately not part of the report's samples.
    is_v7b = _is_v7b_packet(packet)
    if is_v7b:
        from src.shared.codec.topos_binding import TC_DECODE_MODE_AUTO_2K
        geometry = codec.decode_request(
            packet, codec.make_decode_request(TC_DECODE_MODE_AUTO_2K)
        ).info
        source_width, source_height = _v7b_source_geometry(packet)
    else:
        geometry = codec.decode(packet).info
        source_width, source_height = (
            int(geometry.visible_width), int(geometry.visible_height)
        )
    divisor = int(args.scale)
    target_width = (source_width + divisor - 1) // divisor
    target_height = (source_height + divisor - 1) // divisor
    source.update({
        "width": source_width,
        "height": source_height,
        "plane_count": int(geometry.plane_count),
        "pixel_format": int(geometry.pixel_format),
        "bit_depth": int(geometry.bit_depth),
        "packet_sha256": hashlib.sha256(packet).hexdigest(),
        "packet_bytes": len(packet),
    })

    modes = args.modes or list(MODE_NAMES)
    mode_reports = []
    for mode in modes:
        mode_reports.append({
            "name": mode,
            "cold": _run_state(codec, packet, mode, divisor,
                               (target_width, target_height), "cold", 1, 0,
                               is_v7b),
            "hot": _run_state(codec, packet, mode, divisor,
                              (target_width, target_height), "hot",
                              int(args.iterations), int(args.warmup), is_v7b),
        })
    return {
        "schema_version": 1,
        "benchmark": "topos_reduced_decode",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "os": platform.platform(),
            "python": platform.python_version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "logical_cpu_count": os.cpu_count() or 1,
            "build_type": os.environ.get("TOPOS_CODEC_BUILD", "release"),
            "git_commit": _git_commit(),
        },
        "source": source,
        "request": {
            "scale_divisor": divisor,
            "scale": SCALE_BY_DIVISOR[divisor],
            "target_width": target_width,
            "target_height": target_height,
        },
        "threads": {
            "requested": int(args.threads or 0),
            "effective": int(codec.slice_threads()),
            "env": os.environ.get("TOPOS_SLICE_THREADS"),
        },
        "cache_policy": {
            "cold": "first timed call after process warm-up; OS cache is not flushed",
            "hot": f"{args.warmup} warm-up calls followed by {args.iterations} measured calls",
        },
        "modes": mode_reports,
        "notes": [
            "legacy_reduced still consumes legacy V1/V2 variable-length payloads; only coefficient reconstruction is reduced",
            "v7_base uses the V7-B AUTO_2K request and does not read enhancement residual payloads",
            "v7_band remains an explicit unsupported row until a public V7-A packet source is available",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--packet", type=Path, help="existing elementary .toos frame packet")
    inputs.add_argument("--source", type=Path, help="BGR/BGRA PNG/TIFF; encode once before benchmark")
    parser.add_argument("--scalable", action="store_true",
                        help="encode --source as an opt-in V7-B base+residual packet")
    parser.add_argument("--base-max-dim", type=int, default=2048,
                        help="V7-B base longest edge (default: 2048)")
    parser.add_argument("--scale", type=int, choices=tuple(SCALE_BY_DIVISOR), default=3,
                        help="preview divisor (2, 3, 4 or 8)")
    parser.add_argument("--threads", type=int, default=None,
                        help="TOPOS_SLICE_THREADS value; omit to use library default")
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--mode", dest="modes", action="append", choices=MODE_NAMES,
                        help="mode to run; repeatable, defaults to all five")
    parser.add_argument("--output", type=Path, help="write JSON report here; otherwise stdout")
    args = parser.parse_args(argv)
    if args.iterations < 1 or args.warmup < 0:
        parser.error("--iterations must be >= 1 and --warmup must be >= 0")
    if args.threads is not None and args.threads < 1:
        parser.error("--threads must be >= 1")
    if args.scalable and args.packet is not None:
        parser.error("--scalable is only valid with --source")
    if args.base_max_dim < 1 or args.base_max_dim > 2048:
        parser.error("--base-max-dim must be in 1..2048")
    for path in (args.packet, args.source):
        if path is not None and not path.is_file():
            parser.error(f"input file does not exist: {path}")
    report = run(args)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
