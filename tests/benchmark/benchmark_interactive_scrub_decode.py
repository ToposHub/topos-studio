#!/usr/bin/env python3
"""
Interactive scrub decode benchmark for real test videos.

This benchmark exercises the same latest-only decode entrypoint used by the
edit preview worker:

    EditGpuDecodeManager.estimate_interactive_decode_deadline_ms()
    EditGpuDecodeManager.get_frame_interactive()

It intentionally does not create UI widgets. The goal is to measure whether a
real 2K/4K source can return clear native/hardware-decoded frames quickly when
the playhead jumps around.

Usage:
    .venv/bin/python tests/benchmark/benchmark_interactive_scrub_decode.py \
      --videos tests/test_videos \
      --steps 80 \
      --target-size 1280x720 \
      --warmup-wait-ms 2500 \
      --output-json tests/media/results/interactive_scrub_decode.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv"}


def _probe_frame_count(video_path: Path) -> int:
    try:
        import av

        container = av.open(str(video_path))
        stream = container.streams.video[0]
        frames = int(stream.frames or 0)
        if frames <= 0 and stream.duration and stream.time_base and stream.average_rate:
            duration_s = float(stream.duration * stream.time_base)
            frames = int(duration_s * float(stream.average_rate))
        container.close()
        return max(frames, 1)
    except Exception:
        return 300


def _iter_video_files(path: Path) -> Iterable[Path]:
    if path.is_file():
        if path.suffix.lower() in VIDEO_EXTENSIONS:
            yield path
        return
    for child in sorted(path.iterdir()):
        if child.is_file() and child.suffix.lower() in VIDEO_EXTENSIONS:
            yield child


def _parse_target_size(value: str) -> tuple[int, int] | None:
    if value.lower() in {"native", "none", "full"}:
        return None
    if "x" not in value.lower():
        raise argparse.ArgumentTypeError("target size must look like 1280x720 or native")
    left, right = value.lower().split("x", 1)
    width, height = int(left), int(right)
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("target size must be positive")
    return width, height


def _scrub_targets(frame_count: int, steps: int) -> list[int]:
    if frame_count <= 1:
        return [0] * max(1, steps)
    span = frame_count - 1
    anchors = [0, span // 4, span // 2, (span * 3) // 4, span]
    targets: list[int] = []
    for i in range(max(1, steps)):
        if i % 5 == 0:
            targets.append(anchors[(i // 5) % len(anchors)])
        elif i % 2 == 0:
            targets.append((i * 37) % span)
        else:
            targets.append(span - ((i * 53) % span))
    return targets


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[index]


def benchmark_video(
    video_path: Path,
    *,
    steps: int,
    target_size: tuple[int, int] | None,
    warmup_wait_ms: float,
    preposition: bool,
    preposition_deadline_ms: float,
    preposition_lead_ms: float,
) -> dict[str, Any]:
    manager = EditGpuDecodeManager()
    frame_count = _probe_frame_count(video_path)
    targets = _scrub_targets(frame_count, steps)
    stream_key = f"scrub-bench:{video_path}"

    warmup_ready = False
    if warmup_wait_ms > 0.0:
        manager.warmup_source(str(video_path), target_size=target_size, stream_key=stream_key)
        warmup_ready = manager.wait_for_warmups(timeout=warmup_wait_ms / 1000.0)

    samples: list[dict[str, Any]] = []
    for target in targets:
        preposition_requested = False
        if preposition:
            preposition_requested = manager.preposition_interactive_frame(
                str(video_path),
                target,
                target_size=target_size,
                stream_key=stream_key,
                deadline_ms=preposition_deadline_ms,
            )
            if preposition_lead_ms > 0.0:
                time.sleep(preposition_lead_ms / 1000.0)
        deadline_ms = manager.estimate_interactive_decode_deadline_ms(
            str(video_path),
            target,
            target_size=target_size,
            stream_key=stream_key,
        )
        start = time.perf_counter()
        frame, frame_type = manager.get_frame_interactive(
            str(video_path),
            target,
            target_size=target_size,
            stream_key=stream_key,
            is_stale=lambda: False,
            deadline_at=start + deadline_ms / 1000.0,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        delivered = frame is not None
        decoded_frame = getattr(frame, "frame_index", None)
        frame_error = (
            abs(int(decoded_frame) - int(target))
            if delivered and isinstance(decoded_frame, int)
            else None
        )
        samples.append(
            {
                "target": target,
                "delivered": delivered,
                "frame_type": frame_type,
                "decoded_frame": decoded_frame,
                "frame_error": frame_error,
                "deadline_ms": deadline_ms,
                "elapsed_ms": elapsed_ms,
                "deadline_miss": elapsed_ms > deadline_ms,
                "preposition_requested": preposition_requested,
            }
        )

    latencies = [s["elapsed_ms"] for s in samples if s["delivered"]]
    deadline_misses = sum(1 for s in samples if s["deadline_miss"])
    delivered_count = sum(1 for s in samples if s["delivered"])
    frame_types = Counter(str(s["frame_type"]) for s in samples)
    frame_errors = [
        int(s["frame_error"])
        for s in samples
        if s.get("frame_error") is not None
    ]
    exact_frames = sum(1 for error in frame_errors if error == 0)
    near_frames = sum(1 for error in frame_errors if error <= 12)

    return {
        "video_path": str(video_path),
        "frame_count": frame_count,
        "target_size": f"{target_size[0]}x{target_size[1]}" if target_size else "native",
        "steps": len(samples),
        "warmup_wait_ms": warmup_wait_ms,
        "warmup_ready": warmup_ready,
        "preposition": preposition,
        "preposition_deadline_ms": preposition_deadline_ms,
        "preposition_lead_ms": preposition_lead_ms,
        "summary": {
            "delivered": delivered_count,
            "dropped": len(samples) - delivered_count,
            "delivered_ratio": delivered_count / max(1, len(samples)),
            "deadline_misses": deadline_misses,
            "deadline_miss_ratio": deadline_misses / max(1, len(samples)),
            "latency_p50_ms": statistics.median(latencies) if latencies else 0.0,
            "latency_p90_ms": _percentile(latencies, 90),
            "latency_p95_ms": _percentile(latencies, 95),
            "latency_max_ms": max(latencies) if latencies else 0.0,
            "exact_frames": exact_frames,
            "exact_ratio": exact_frames / max(1, delivered_count),
            "near_frames_12": near_frames,
            "near_ratio_12": near_frames / max(1, delivered_count),
            "frame_error_p95": _percentile(frame_errors, 95),
            "frame_error_max": max(frame_errors) if frame_errors else 0,
            "frame_types": dict(frame_types),
            "preposition_requested": sum(
                1 for sample in samples if sample.get("preposition_requested")
            ),
        },
        "samples": samples,
    }


def aggregate_results(video_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a compact cross-video summary for regression tracking."""
    total_steps = sum(int(result.get("steps", 0)) for result in video_results)
    delivered = sum(int(result.get("summary", {}).get("delivered", 0)) for result in video_results)
    deadline_misses = sum(
        int(result.get("summary", {}).get("deadline_misses", 0))
        for result in video_results
    )
    frame_types: Counter[str] = Counter()
    delivered_latencies: list[float] = []
    frame_errors: list[int] = []
    for result in video_results:
        frame_types.update(result.get("summary", {}).get("frame_types", {}))
        for sample in result.get("samples", []):
            if sample.get("delivered"):
                delivered_latencies.append(float(sample.get("elapsed_ms", 0.0)))
                if sample.get("frame_error") is not None:
                    frame_errors.append(int(sample.get("frame_error", 0)))

    vt_delivered = sum(
        count
        for frame_type, count in frame_types.items()
        if str(frame_type).startswith("vt_")
    )
    return {
        "videos": len(video_results),
        "steps": total_steps,
        "delivered": delivered,
        "dropped": total_steps - delivered,
        "delivered_ratio": delivered / max(1, total_steps),
        "deadline_misses": deadline_misses,
        "deadline_miss_ratio": deadline_misses / max(1, total_steps),
        "vt_delivered": vt_delivered,
        "vt_delivered_ratio": vt_delivered / max(1, delivered),
        "latency_p50_ms": statistics.median(delivered_latencies) if delivered_latencies else 0.0,
        "latency_p90_ms": _percentile(delivered_latencies, 90),
        "latency_p95_ms": _percentile(delivered_latencies, 95),
        "latency_max_ms": max(delivered_latencies) if delivered_latencies else 0.0,
        "exact_frames": sum(1 for error in frame_errors if error == 0),
        "exact_ratio": (
            sum(1 for error in frame_errors if error == 0) / max(1, delivered)
        ),
        "near_frames_12": sum(1 for error in frame_errors if error <= 12),
        "near_ratio_12": (
            sum(1 for error in frame_errors if error <= 12) / max(1, delivered)
        ),
        "frame_error_p95": _percentile(frame_errors, 95),
        "frame_error_max": max(frame_errors) if frame_errors else 0,
        "frame_types": dict(frame_types),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--videos",
        default=str(REPO_ROOT / "tests" / "test_videos"),
        help="Video file or directory. Defaults to tests/test_videos.",
    )
    parser.add_argument("--steps", type=int, default=80, help="Number of scrub jumps per video.")
    parser.add_argument(
        "--target-size",
        type=_parse_target_size,
        default=(1280, 720),
        help="Preview decode target, e.g. 1280x720, 1920x1080, or native.",
    )
    parser.add_argument(
        "--warmup-wait-ms",
        type=float,
        default=0.0,
        help="Pre-create the decode source and wait this many ms before measuring.",
    )
    parser.add_argument(
        "--preposition",
        action="store_true",
        help="Preposition a spare interactive VT source before each measured scrub decode.",
    )
    parser.add_argument(
        "--preposition-deadline-ms",
        type=float,
        default=48.0,
        help="Decode deadline for each preposition attempt.",
    )
    parser.add_argument(
        "--preposition-lead-ms",
        type=float,
        default=24.0,
        help="Lead time between preposition request and measured decode.",
    )
    parser.add_argument("--output-json", default="", help="Optional JSON output path.")
    args = parser.parse_args()

    video_root = Path(args.videos).expanduser()
    videos = list(_iter_video_files(video_root))
    if not videos:
        print(f"No video files found under {video_root}", file=sys.stderr)
        return 2

    video_results = [
        benchmark_video(
            video,
            steps=args.steps,
            target_size=args.target_size,
            warmup_wait_ms=args.warmup_wait_ms,
            preposition=bool(args.preposition),
            preposition_deadline_ms=max(1.0, args.preposition_deadline_ms),
            preposition_lead_ms=max(0.0, args.preposition_lead_ms),
        )
        for video in videos
    ]

    results = {
        "timestamp": datetime.now().isoformat(),
        "config": {
            "videos": str(video_root),
            "steps": args.steps,
            "target_size": (
                f"{args.target_size[0]}x{args.target_size[1]}"
                if args.target_size
                else "native"
            ),
            "warmup_wait_ms": args.warmup_wait_ms,
            "preposition": bool(args.preposition),
            "preposition_deadline_ms": max(1.0, args.preposition_deadline_ms),
            "preposition_lead_ms": max(0.0, args.preposition_lead_ms),
        },
        "summary": aggregate_results(video_results),
        "videos": video_results,
    }

    text = json.dumps(results, indent=2, ensure_ascii=False)
    if args.output_json:
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
