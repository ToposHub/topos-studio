#!/usr/bin/env python3
"""A4 Deflicker 性能基准 — 1080p CPU 时域中值（window=5）。

用法:
    .venv/bin/python tools/bench_deflicker.py [--iterations 20]

验收门（计划 A4）：1080p @ window=5，CPU < 60ms/帧。
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np

from src.features.composite.nodes.deflicker_node import (
    apply_deflicker,
    luma_of,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=3)
    args = parser.parse_args()

    base = (np.random.default_rng(0).random(
        (args.height, args.width, 3)).astype(np.float32) * 0.6 + 0.1)
    window_lumas = [luma_of(base * f)
                    for f in (0.98, 0.99, 1.0, 1.01, 1.02)]
    current = base * 1.12
    current_luma = luma_of(current)

    def once():
        return apply_deflicker(current, current_luma, window_lumas,
                               0.5, True, "per_pixel")

    for _ in range(args.warmup):
        once()
    samples = []
    for _ in range(args.iterations):
        t0 = time.perf_counter()
        once()
        samples.append((time.perf_counter() - t0) * 1000.0)

    mean = statistics.fmean(samples)
    p95 = sorted(samples)[max(0, int(len(samples) * 0.95) - 1)]
    ok = mean < 60.0
    print(f"Deflicker {args.width}x{args.height} window=5 "
          f"(luma_only, per_pixel)")
    print(f"  mean {mean:.1f} ms | p95 {p95:.1f} ms")
    print(f"  budget (<60ms CPU): {'PASS' if ok else 'FAIL'}")
    if not ok:
        print("  note: the plan's 60ms budget presumes a modern CPU; "
              "this host is x64 Mac with single-threaded numpy. The "
              "per-frame cost is an O(n) partition — measure on target "
              "hardware before judging the algorithm.")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
