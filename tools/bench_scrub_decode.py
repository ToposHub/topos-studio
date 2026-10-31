#!/usr/bin/env python
"""Benchmark: interactive decode latency + simulated fast scrub with the
real staleness policy used by PreviewRenderWorker (grace=16ms token kill).

Run: .venv/bin/python tools/bench_scrub_decode.py [--quick]
"""
from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager

VIDEOS = {
    "4K_H264": os.path.join(ROOT, "tests/test_videos/4K_H264.mov"),
    "4K_H265": os.path.join(ROOT, "tests/test_videos/4K_H265.mov"),
    "4K_prores422HQ": os.path.join(ROOT, "tests/test_videos/4K_prores422HQ.mov"),
    "4K_DNxHR": os.path.join(ROOT, "tests/test_videos/4K_DNxHR HQX 12-bit.mov"),
}


def bench_step_latency(mgr: EditGpuDecodeManager, path: str, quick: bool) -> None:
    name = os.path.basename(path)
    # warm up source creation + first decode
    t0 = time.perf_counter()
    frame, ftype = mgr.get_frame_interactive(path, 100, stream_key="bench")
    warm_ms = (time.perf_counter() - t0) * 1000.0
    print(f"\n[{name}] warmup frame_type={ftype} {warm_ms:.1f}ms")

    steps = [1, -1, 5, 10, 30, 100, 300] if not quick else [1, 30, 300]
    ref = 100
    results = []
    for step in steps:
        target = ref + step
        t0 = time.perf_counter()
        frame, ftype = mgr.get_frame_interactive(
            path, target, stream_key="bench",
            is_stale=lambda: False,
            deadline_at=time.perf_counter() + 10.0,
        )
        ms = (time.perf_counter() - t0) * 1000.0
        ok = frame is not None
        results.append((step, ms, ftype, ok))
        # return to ref so each measurement starts from the same cursor state
        mgr.get_frame_interactive(
            path, ref, stream_key="bench",
            is_stale=lambda: False,
            deadline_at=time.perf_counter() + 10.0,
        )
    print(f"[{name}] step latency from frame {ref}:")
    for step, ms, ftype, ok in results:
        print(f"   {step:+4d} frames -> {ms:7.1f} ms  ({ftype or 'ABORT'})")


def _simulate_scrub(mgr: EditGpuDecodeManager, path: str, *, req_interval_ms: float,
                    frames_per_req: int, grace_ms: float, duration_s: float,
                    start: int = 100) -> dict:
    """Replicate worker's _is_stale_interactive_scrub_task + single-flight loop.

    UI thread: every req_interval_ms a new scrub request arrives with a fresh
    token (this matches EditPage 4ms coalescing + mouse-move rate).
    Worker: decodes latest target; is_stale mirrors the real callback:
        token != latest_token AND age >= grace_ms  -> abort mid-decode.
    """
    token = 0
    latest_token = 0
    latest_target = start
    displayed = 0
    aborted = 0
    deadline_miss = 0
    t_start = time.perf_counter()
    next_req_at = t_start
    last_decode_end = t_start

    while True:
        now = time.perf_counter()
        if now - t_start >= duration_s:
            break
        # UI issues a newer request
        if now >= next_req_at:
            latest_token += 1
            latest_target += frames_per_req
            next_req_at = now + req_interval_ms / 1000.0

        # worker idle -> pick up latest target
        if now >= last_decode_end:
            token = latest_token
            target = latest_target
            req_started = time.perf_counter()

            def is_stale(tk=token, rs=req_started) -> bool:
                if latest_token != tk and (time.perf_counter() - rs) * 1000.0 >= grace_ms:
                    return True
                return False

            # deadline replicates the worker's estimate-based deadline
            deadline = req_started + 0.160
            t0 = time.perf_counter()
            frame, ftype = mgr.get_frame_interactive(
                path, max(0, min(target, 740)), stream_key="bench",
                is_stale=is_stale, deadline_at=deadline,
            )
            ms = (time.perf_counter() - t0) * 1000.0
            last_decode_end = time.perf_counter()
            if frame is None:
                if ftype == "interactive_stale":
                    aborted += 1
                else:
                    deadline_miss += 1
            else:
                displayed += 1
        else:
            time.sleep(0.001)

    total = displayed + aborted + deadline_miss
    return {
        "requests_seen": latest_token,
        "decoded_ok": displayed,
        "aborted_stale": aborted,
        "deadline_or_other": deadline_miss,
        "display_fps": displayed / duration_s,
        "final_gap": latest_target - max(0, min(latest_target, 740)),
        "total": total,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()

    for name, path in VIDEOS.items():
        if not os.path.exists(path):
            print(f"skip missing {path}")
            continue
        mgr = EditGpuDecodeManager()
        bench_step_latency(mgr, path, args.quick)

        scenarios = [
            ("slow drag (1f/100ms, grace=16)", dict(req_interval_ms=100.0, frames_per_req=1, grace_ms=16.0, duration_s=3.0)),
            ("fast drag (2f/8ms, grace=16 == CURRENT)", dict(req_interval_ms=8.0, frames_per_req=2, grace_ms=16.0, duration_s=3.0)),
            ("fast drag (2f/8ms, grace=INF == FIXED)", dict(req_interval_ms=8.0, frames_per_req=2, grace_ms=1e9, duration_s=3.0)),
        ]
        for label, kw in scenarios:
            # fresh manager per scenario so cursor pools don't help across runs
            mgr2 = EditGpuDecodeManager()
            mgr2.get_frame_interactive(path, 100, stream_key="bench")
            r = _simulate_scrub(mgr2, path, **kw)
            print(f"[{name}] {label:42s} decoded={r['decoded_ok']:3d} ({r['display_fps']:5.1f}fps) "
                  f"aborted={r['aborted_stale']:3d} deadline/other={r['deadline_or_other']:3d}")
            mgr2.close_all()
        mgr.close_all()


if __name__ == "__main__":
    main()
