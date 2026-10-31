#!/usr/bin/env python3
"""A3 benchmark: parallel export speedup (N=1 vs N=2 vs N=4).

Renders a synthetic N-frame job through the A3 parallel driver with fake
CPU-bound "renderers" (representative of graph evaluation cost) and
reports the wall-clock speedup. For end-to-end GPU numbers run a real
composite export with composite.export_parallel_frames set to 1 / 2 / 4
and compare the exporter's reported duration — GL context creation can't
run in CI, so this tool isolates the pipeline overhead/speedup.

Usage:
    python tools/bench_parallel_export.py [--frames 100] [--size 1920x1080]
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np

from src.features.composite.render.parallel_frame_render import (
    render_frames_parallel, resolve_parallel_workers)


def _synthetic_render(w: int, h: int, work_ms: float):
    """Fake renderer: CPU-bound array math approximating per-frame cost."""
    def render(frame_no: int) -> np.ndarray:
        img = np.random.rand(h // 4, w // 4, 3).astype(np.float32)
        # busy-work proportional to the requested cost
        iters = max(1, int(work_ms / 2))
        for _ in range(iters):
            img = np.sin(img * 1.0001 + frame_no * 0.001)
        return img
    return render


def bench(frames: int, w: int, h: int, work_ms: float, workers: int) -> float:
    durations = []
    state = {"t0": None}

    def factory(_index):
        return _synthetic_render(w, h, work_ms)

    def sink(frame_no, payload):
        pass  # encode/write excluded from the render-cost comparison

    start = time.perf_counter()
    ok = render_frames_parallel(
        list(range(frames)), factory, sink, workers,
        progress_cb=lambda cur, total: None)
    elapsed = time.perf_counter() - start
    if not ok:
        raise RuntimeError(f"parallel run failed at N={workers}")
    return elapsed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=100)
    ap.add_argument("--size", type=str, default="1920x1080")
    ap.add_argument("--work-ms", type=float, default=8.0,
                    help="per-frame render cost (CPU bound)")
    args = ap.parse_args()
    w, h = (int(v) for v in args.size.split("x"))

    results = {}
    for n in (1, 2, 4):
        # N=1 declines inside the driver (serial baseline) — measure inline.
        if n == 1:
            render = _synthetic_render(w, h, args.work_ms)
            t0 = time.perf_counter()
            for f in range(args.frames):
                render(f)
            results[n] = time.perf_counter() - t0
        else:
            n_eff = resolve_parallel_workers(n, os.cpu_count() or 1)
            results[n] = bench(args.frames, w, h, args.work_ms, n_eff)

    base = results[1]
    print(f"frames={args.frames} size={args.size} work={args.work_ms}ms "
          f"cpus={os.cpu_count()}")
    for n, elapsed in results.items():
        print(f"N={n}: {elapsed:8.2f}s  speedup={base / elapsed:5.2f}x")


if __name__ == "__main__":
    main()
