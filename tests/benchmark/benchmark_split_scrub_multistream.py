#!/usr/bin/env python3
"""Multi-segment scrub benchmark on REAL footage.

Simulates the exact production scrub pipeline entrypoint used by both the
Edit page and the Color page timeline preview (TimelinePreviewRenderer ->
PreviewRenderWorker -> EditGpuDecodeManager.get_frame_interactive).

Scenarios:
  single   - one stream key over the whole source (= one un-split clip, or
             blade-split contiguous segments which share the stream key)
  multi4   - 4 stream keys on the SAME file, scrub pattern crossing between
             them (= same media split into 4 non-contiguous segments:
             gaps / ripple-deleted middles / reordering all produce this)
  multi4-warm - same as multi4 but every stream key pre-warmed up front,
             isolating "VT session count" cost from "warmup pending" drops.

For every scenario we report delivered ratio (frames actually shown),
latency percentiles, and frame type breakdown. Production deadline logic
mirrors TimelinePreviewRenderer (_interactive_scrub_decode_deadline_ms=160,
estimate_interactive_decode_deadline_ms, is_stale=latest-frame check).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager


def probe_frame_count(video_path: Path) -> int:
    import av

    container = av.open(str(video_path))
    stream = container.streams.video[0]
    frames = int(stream.frames or 0)
    container.close()
    return max(frames, 1)


def scrub_pattern(frame_count: int, steps: int, segments: list[range]) -> list[tuple[int, str]]:
    """Scrub pattern as (source_target, stream_key) pairs.

    Mirrors dragging the playhead across a timeline that contains `segments`
    (each entry: (source-frame range, its stream key)). While the playhead
    sweeps forward the *source* frame alternates between the segments'
    regions whenever their timeline order differs from source order.
    """
    targets: list[tuple[int, str]] = []
    span = max(1, steps)
    for i in range(span):
        seg, key = segments[i % len(segments)]
        pos = int((i / span) * (seg.stop - seg.start)) + seg.start
        # alternate direction per pass to mimic back-and-forth drags
        if (i // len(segments)) % 2 == 1:
            pos = seg.stop - (pos - seg.start) - 1
        targets.append((max(0, min(pos, frame_count - 1)), key))
    return targets


def run_scenario(
    manager: EditGpuDecodeManager,
    video_path: Path,
    stream_keys: list[str],
    targets: list[tuple[int, str]],
    *,
    warm_all: bool,
    prewarm_wait_s: float,
    coarse: bool,
) -> dict:
    if warm_all:
        for key in stream_keys:
            manager.warmup_source(str(video_path), target_size=None, stream_key=key)
        manager.wait_for_warmups(timeout=prewarm_wait_s)

    samples = []
    for target, key in targets:
        deadline_ms = manager.estimate_interactive_decode_deadline_ms(
            str(video_path), target, target_size=None, stream_key=key
        )
        deadline_ms = max(deadline_ms, 160.0)
        start = time.perf_counter()
        frame, frame_type = manager.get_frame_interactive(
            str(video_path),
            target,
            target_size=None,
            stream_key=key,
            is_stale=lambda: False,
            deadline_at=start + deadline_ms / 1000.0,
            coarse=coarse,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        samples.append(
            {
                "target": target,
                "key": key,
                "delivered": frame is not None,
                "frame_type": str(frame_type),
                "elapsed_ms": elapsed_ms,
            }
        )
    latencies = [s["elapsed_ms"] for s in samples if s["delivered"]]
    types = Counter(s["frame_type"] for s in samples)
    return {
        "stream_keys": len(set(stream_keys)),
        "steps": len(samples),
        "delivered": sum(1 for s in samples if s["delivered"]),
        "delivered_ratio": sum(1 for s in samples if s["delivered"]) / max(1, len(samples)),
        "latency_p50_ms": statistics.median(latencies) if latencies else 0.0,
        "latency_p90_ms": sorted(latencies)[int(0.9 * (len(latencies) - 1))] if latencies else 0.0,
        "latency_max_ms": max(latencies) if latencies else 0.0,
        "frame_types": dict(types),
        "samples": samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True)
    parser.add_argument("--steps", type=int, default=120)
    parser.add_argument("--segments", type=int, default=4)
    parser.add_argument("--coarse", action="store_true", help="Fast-drag coarse (keyframe-anchored) mode.")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    video = Path(args.video)
    frame_count = probe_frame_count(video)
    seg_len = frame_count // args.segments
    regions = [range(i * seg_len, (i + 1) * seg_len) for i in range(args.segments)]
    keys = [f"seg-{i}" for i in range(args.segments)]

    # Source order scrambled relative to timeline order: the first timeline
    # segment shows the LAST quarter of the media, etc. Common after
    # cutting + reordering (or ripple-deleting middles, which leaves gaps).
    shuffled = [(seg, keys[i]) for i, seg in enumerate(reversed(regions))]
    targets_single = scrub_pattern(frame_count, args.steps, [(range(0, frame_count), "whole")])
    targets_multi = scrub_pattern(frame_count, args.steps, shuffled)

    results = {}
    m1 = EditGpuDecodeManager()
    results["single"] = run_scenario(
        m1, video, ["whole"], targets_single,
        warm_all=False, prewarm_wait_s=0, coarse=args.coarse,
    )
    m2 = EditGpuDecodeManager()
    results["multi_cold"] = run_scenario(
        m2, video, keys, targets_multi,
        warm_all=False, prewarm_wait_s=0, coarse=args.coarse,
    )
    m3 = EditGpuDecodeManager()
    results["multi_warm"] = run_scenario(
        m3, video, keys, targets_multi,
        warm_all=True, prewarm_wait_s=8.0, coarse=args.coarse,
    )
    for m in (m1, m2, m3):
        try:
            m.shutdown()
        except Exception:
            pass

    summary = {
        "video": video.name,
        "frame_count": frame_count,
        "segments": args.segments,
        "steps": args.steps,
        "coarse": args.coarse,
        "scenarios": {
            name: {k: v for k, v in r.items() if k != "samples"} for name, r in results.items()
        },
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
