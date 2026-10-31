#!/usr/bin/env python3
"""
Performance benchmark for Timeline UI operations.

Measures performance of core model operations that underpin timeline UI:
- TimelineSequence immutable operations (add/remove/move clips)
- CommandStack apply/undo throughput
- TimelineLayoutEngine hit-test and layout computation
- Large timeline stress test (1000+ clips)

Usage:
    .venv/bin/python tools/benchmark_timeline_ui.py
    .venv/bin/python tools/benchmark_timeline_ui.py --clips 5000
    .venv/bin/python tools/benchmark_timeline_ui.py --json
"""

import sys
import os
import time
import argparse
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.features.edit.core.timeline_v2 import (
    TimelineSequence, EditTrack, ClipInstance,
)
from src.features.edit.core.commands import (
    AddClipCommand, MoveClipCommand, DeleteClipCommand,
    CommandStack, MacroCommand,
)
from src.features.edit.ui.timeline_layout import (
    TimelineLayoutEngine, TimelineLayoutState, TrackInfo,
)


def _make_clip(clip_id: str, track_id: str, start: int, duration: int = 100) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id, asset_id=f"asset_{clip_id}", track_id=track_id,
        timeline_start=start, timeline_duration=duration,
        source_start=0, source_duration=duration,
    )


def bench_sequence_add_clips(n_clips: int = 500) -> dict:
    """Benchmark adding clips to a timeline."""
    tl = TimelineSequence(timeline_id="bench", name="Bench", fps=24.0)
    v1 = EditTrack(track_id="track_V1", name="V1", track_type="video", index=0)
    tl = tl.with_track_added(v1)

    t0 = time.perf_counter()
    for i in range(n_clips):
        clip = _make_clip(f"c{i}", v1.track_id, start=i * 100, duration=100)
        tl = tl.with_clip_added(v1.track_id, clip)
    elapsed = time.perf_counter() - t0

    return {
        "name": f"add_{n_clips}_clips",
        "clips": n_clips,
        "elapsed_s": round(elapsed, 4),
        "ops_per_sec": round(n_clips / elapsed, 1),
    }


def bench_command_stack(n_clips: int = 200) -> dict:
    """Benchmark CommandStack execute + undo."""
    tl = TimelineSequence(timeline_id="bench", name="Bench", fps=24.0)
    v1 = EditTrack(track_id="track_V1", name="V1", track_type="video", index=0)
    tl = tl.with_track_added(v1)

    stack = CommandStack()

    commands = []
    for i in range(n_clips):
        clip = _make_clip(f"c{i}", v1.track_id, start=i * 100, duration=100)
        commands.append(AddClipCommand.create(v1.track_id, clip))

    # Execute all
    t0 = time.perf_counter()
    for cmd in commands:
        tl = stack.execute(cmd, tl)
    exec_time = time.perf_counter() - t0

    # Undo all
    t1 = time.perf_counter()
    for _ in range(n_clips):
        tl = stack.undo(tl)
    undo_time = time.perf_counter() - t1

    return {
        "name": f"command_stack_{n_clips}",
        "clips": n_clips,
        "execute_s": round(exec_time, 4),
        "undo_s": round(undo_time, 4),
        "exec_ops_per_sec": round(n_clips / exec_time, 1),
        "undo_ops_per_sec": round(n_clips / undo_time, 1),
    }


def bench_layout_engine(n_tracks: int = 10, n_hit_tests: int = 10000) -> dict:
    """Benchmark layout engine configuration and hit testing."""
    engine = TimelineLayoutEngine()
    tracks = []
    for i in range(n_tracks // 2):
        tracks.append(TrackInfo(track_id=f"track_V{i+1}", track_type="video"))
    for i in range(n_tracks // 2):
        tracks.append(TrackInfo(track_id=f"track_A{i+1}", track_type="audio"))

    state = TimelineLayoutState()

    t0 = time.perf_counter()
    engine.configure(tracks, state, 1920, 1080, 5.0)
    config_time = time.perf_counter() - t0

    # Hit tests
    t1 = time.perf_counter()
    for x in range(0, n_hit_tests):
        engine.x_to_frame(x * 0.5)
    hit_time = time.perf_counter() - t1

    return {
        "name": f"layout_engine_{n_tracks}t",
        "tracks": n_tracks,
        "config_ms": round(config_time * 1000, 2),
        "hit_tests": n_hit_tests,
        "hit_test_ms": round(hit_time * 1000, 2),
        "hits_per_sec": round(n_hit_tests / hit_time, 1),
    }


def bench_move_clip(n_clips: int = 500, n_moves: int = 200) -> dict:
    """Benchmark moving clips within a populated timeline."""
    tl = TimelineSequence(timeline_id="bench", name="Bench", fps=24.0)
    v1 = EditTrack(track_id="track_V1", name="V1", track_type="video", index=0)
    tl = tl.with_track_added(v1)

    # Populate
    for i in range(n_clips):
        clip = _make_clip(f"c{i}", v1.track_id, start=i * 100, duration=100)
        tl = tl.with_clip_added(v1.track_id, clip)

    # Move first 200 clips by +50 frames
    t0 = time.perf_counter()
    for i in range(min(n_moves, n_clips)):
        clip = tl.get_clip_by_id(f"c{i}")
        if clip:
            cmd = MoveClipCommand.create(
                v1.track_id, clip.clip_id,
                clip.timeline_start, clip.timeline_start + 50,
            )
            tl = cmd.apply(tl)
    elapsed = time.perf_counter() - t0

    return {
        "name": f"move_{n_moves}_of_{n_clips}_clips",
        "moves": n_moves,
        "total_clips": n_clips,
        "elapsed_s": round(elapsed, 4),
        "ops_per_sec": round(n_moves / elapsed, 1),
    }


# ── Performance Thresholds ──

PERFORMANCE_THRESHOLDS = {
    # ops_per_sec: higher is better
    "add_clips_ops_per_sec": 5000,
    "command_stack_ops_per_sec": 2000,
    "layout_hits_per_sec": 10000,
    "move_ops_per_sec": 3000,
}


def evaluate_thresholds(benchmarks: list[dict]) -> list[dict]:
    """Evaluate each benchmark against thresholds and add pass/fail status."""
    results = []
    for b in benchmarks:
        result = dict(b)
        passed = True
        failures = []
        name = b.get("name", "")

        if "add_" in name and "clips" in name:
            threshold = PERFORMANCE_THRESHOLDS["add_clips_ops_per_sec"]
            val = b.get("ops_per_sec", 0)
            if val < threshold:
                passed = False
                failures.append(f"ops_per_sec {val} < {threshold}")

        elif "command_stack" in name:
            threshold = PERFORMANCE_THRESHOLDS["command_stack_ops_per_sec"]
            val = b.get("exec_ops_per_sec", 0)
            if val < threshold:
                passed = False
                failures.append(f"exec_ops_per_sec {val} < {threshold}")

        elif "layout" in name:
            threshold = PERFORMANCE_THRESHOLDS["layout_hits_per_sec"]
            val = b.get("hits_per_sec", 0)
            if val < threshold:
                passed = False
                failures.append(f"hits_per_sec {val} < {threshold}")

        elif "move" in name:
            threshold = PERFORMANCE_THRESHOLDS["move_ops_per_sec"]
            val = b.get("ops_per_sec", 0)
            if val < threshold:
                passed = False
                failures.append(f"ops_per_sec {val} < {threshold}")

        result["passed"] = passed
        if failures:
            result["failures"] = failures
        results.append(result)

    return results


def main():
    parser = argparse.ArgumentParser(description="Timeline UI Performance Benchmark")
    parser.add_argument("--clips", type=int, default=500, help="Number of clips for benchmarks")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    n = args.clips

    benchmarks = [
        bench_sequence_add_clips(n),
        bench_command_stack(min(n, 200)),
        bench_layout_engine(),
        bench_move_clip(n, min(n, 200)),
    ]

    results = evaluate_thresholds(benchmarks)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print("=" * 60)
        print("Timeline UI Performance Benchmark")
        print(f"Clips parameter: {n}")
        print("=" * 60)
        for b in results:
            status = "PASS" if b.get("passed", True) else "FAIL"
            print(f"\n[{b['name']}] {status}")
            for k, v in b.items():
                if k not in ("name", "passed", "failures"):
                    print(f"  {k}: {v}")
            if "failures" in b:
                for f in b["failures"]:
                    print(f"  FAILED: {f}")

        all_passed = all(b.get("passed", True) for b in results)
        print(f"\n{'=' * 60}")
        print(f"Overall: {'PASS' if all_passed else 'FAIL'}")
        print("Benchmark complete.")


if __name__ == "__main__":
    main()
