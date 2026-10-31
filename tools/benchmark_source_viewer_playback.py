#!/usr/bin/env python3
"""
Performance benchmark for Source Viewer playback operations.

Measures performance of EditPlaybackController:
- Frame pacing (tick) throughput at different FPS values
- Seek latency
- Range playback loop detection
- State transition overhead

Usage:
    .venv/bin/python tools/benchmark_source_viewer_playback.py
    .venv/bin/python tools/benchmark_source_viewer_playback.py --json
"""

import sys
import os
import time
import argparse
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.features.edit.core.edit_playback_controller import (
    EditPlaybackController,
    PlaybackState,
)


def bench_tick_throughput(fps: float = 24.0, n_ticks: int = 10000) -> dict:
    """Benchmark tick throughput at given FPS."""
    c = EditPlaybackController()
    c.set_fps(fps)
    c.set_timeline_duration(240000)
    c.play()

    t0 = time.perf_counter()
    base = time.monotonic()
    for i in range(n_ticks):
        elapsed = i * (1.0 / fps)
        c.tick(base + elapsed)
    elapsed_time = time.perf_counter() - t0

    return {
        "name": f"tick_{fps}fps_{n_ticks}",
        "fps": fps,
        "ticks": n_ticks,
        "elapsed_ms": round(elapsed_time * 1000, 2),
        "ticks_per_sec": round(n_ticks / elapsed_time, 1),
    }


def bench_seek_latency(n_seeks: int = 5000) -> dict:
    """Benchmark seek latency."""
    c = EditPlaybackController()
    c.set_fps(24.0)
    c.set_timeline_duration(24000)

    t0 = time.perf_counter()
    for i in range(n_seeks):
        frame = (i * 7) % 24000  # semi-random pattern
        c.seek(frame)
    elapsed = time.perf_counter() - t0

    return {
        "name": f"seek_{n_seeks}",
        "seeks": n_seeks,
        "elapsed_ms": round(elapsed * 1000, 2),
        "seeks_per_sec": round(n_seeks / elapsed, 1),
    }


def bench_state_transitions(n_transitions: int = 10000) -> dict:
    """Benchmark play/pause/stop transitions."""
    c = EditPlaybackController()
    c.set_fps(24.0)
    c.set_timeline_duration(24000)

    t0 = time.perf_counter()
    for i in range(n_transitions):
        c.play()
        c.pause()
        c.play()
        c.stop()
    elapsed = time.perf_counter() - t0

    return {
        "name": f"transitions_{n_transitions}",
        "transitions": n_transitions * 4,
        "elapsed_ms": round(elapsed * 1000, 2),
        "ops_per_sec": round(n_transitions * 4 / elapsed, 1),
    }


def bench_range_playback(n_loops: int = 1000) -> dict:
    """Benchmark range playback loop detection."""
    c = EditPlaybackController()
    c.set_fps(24.0)
    c.set_timeline_duration(24000)
    c.set_loop(True)
    c.set_playback_range(0, 240)
    c.seek(0)
    c.play()

    t0 = time.perf_counter()
    base = time.monotonic()
    for i in range(n_loops):
        # Simulate each loop cycle: range is 240 frames at 24fps = 10 seconds
        elapsed_per_loop = 240.0 / 24.0
        elapsed = elapsed_per_loop + 0.001
        c.tick(base + elapsed)
    elapsed_time = time.perf_counter() - t0

    return {
        "name": f"range_loop_{n_loops}",
        "loops": n_loops,
        "elapsed_ms": round(elapsed_time * 1000, 2),
        "loops_per_sec": round(n_loops / elapsed_time, 1),
    }


# ── Performance Thresholds ──

PLAYBACK_THRESHOLDS = {
    "ticks_per_sec": 100000,   # tick throughput minimum
    "seeks_per_sec": 50000,    # seek throughput minimum
    "ops_per_sec": 20000,      # state transition ops/sec minimum
    "loops_per_sec": 5000,     # range loop detection minimum
}


def evaluate_playback_thresholds(benchmarks: list[dict]) -> list[dict]:
    """Evaluate each benchmark against thresholds and add pass/fail status."""
    results = []
    for b in benchmarks:
        result = dict(b)
        passed = True
        failures = []

        if "ticks_per_sec" in b:
            threshold = PLAYBACK_THRESHOLDS["ticks_per_sec"]
            if b["ticks_per_sec"] < threshold:
                passed = False
                failures.append(f"ticks_per_sec {b['ticks_per_sec']} < {threshold}")

        if "seeks_per_sec" in b:
            threshold = PLAYBACK_THRESHOLDS["seeks_per_sec"]
            if b["seeks_per_sec"] < threshold:
                passed = False
                failures.append(f"seeks_per_sec {b['seeks_per_sec']} < {threshold}")

        if "ops_per_sec" in b:
            threshold = PLAYBACK_THRESHOLDS["ops_per_sec"]
            if b["ops_per_sec"] < threshold:
                passed = False
                failures.append(f"ops_per_sec {b['ops_per_sec']} < {threshold}")

        if "loops_per_sec" in b:
            threshold = PLAYBACK_THRESHOLDS["loops_per_sec"]
            if b["loops_per_sec"] < threshold:
                passed = False
                failures.append(f"loops_per_sec {b['loops_per_sec']} < {threshold}")

        result["passed"] = passed
        if failures:
            result["failures"] = failures
        results.append(result)

    return results


def main():
    parser = argparse.ArgumentParser(description="Source Viewer Playback Benchmark")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    benchmarks = [
        bench_tick_throughput(24.0, 10000),
        bench_tick_throughput(30.0, 10000),
        bench_tick_throughput(60.0, 10000),
        bench_seek_latency(5000),
        bench_state_transitions(5000),
        bench_range_playback(1000),
    ]

    results = evaluate_playback_thresholds(benchmarks)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print("=" * 60)
        print("Source Viewer Playback Benchmark")
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
