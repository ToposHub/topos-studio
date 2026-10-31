#!/usr/bin/env python3
"""D3 playback + export worker-budget benchmark.

Measures the ToposMediaSource frame-delivery path while a parallel sequential
export-style decoder is active in the same process.  The result records the
cooperative context budgets, so a run that silently oversubscribes the native
slice pool is visible instead of being reported as a single-stream average.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = Path(__file__).resolve().parent
for path in (PROJECT_ROOT, TOOLS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from bench_manifest import ManifestMismatch, build_manifest  # noqa: E402
from bench_topos_playback_path import latency_stats  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--frames", type=int, default=120)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--export-workers", type=int, default=2)
    ap.add_argument("--decoder-workers", type=int, default=-1,
                    help="播放 context：-1=D3 共享预算，0=默认，正数=上限")
    ap.add_argument("--allow-dirty", action="store_true")
    args = ap.parse_args()
    if args.frames <= 0 or args.threads <= 0 or args.export_workers <= 0:
        raise SystemExit("frames/threads/export-workers 必须为正数")
    input_path = Path(args.input)
    if not input_path.is_absolute():
        input_path = PROJECT_ROOT / input_path
    if not input_path.is_file():
        raise SystemExit(f"输入不存在：{input_path}")
    try:
        manifest = build_manifest(allow_dirty=args.allow_dirty)
    except ManifestMismatch as exc:
        print(f"MANIFEST MISMATCH: {exc}", file=sys.stderr)
        return 2

    from src.shared.codec.decode_worker_budget import DECODE_WORKER_BUDGET
    from src.shared.codec.topos_binding import ParallelSequentialDecoder, ToposCodec
    from src.shared.media.topos_source import ToposMediaSource

    codec = ToposCodec()
    codec.set_slice_threads(args.threads)
    decoder_workers = None if args.decoder_workers < 0 else args.decoder_workers
    source = ToposMediaSource(
        str(input_path), decode_workers=decoder_workers, decode_role="playback",
    )
    source.open()
    total = min(args.frames, int(source.total_frames))
    if total <= 0:
        source.close()
        raise SystemExit("输入没有可解码帧")
    warm = source.read_frame(0)
    del warm

    export_error: list[BaseException] = []

    def export_job() -> None:
        try:
            with ParallelSequentialDecoder(
                str(input_path), workers=args.export_workers,
                threads=args.threads, codec=codec,
            ) as export:
                for index, _planes in export:
                    if index >= total - 1:
                        break
        except BaseException as exc:  # return to main thread for clear failure
            export_error.append(exc)

    worker = threading.Thread(target=export_job, name="topos-d3-export", daemon=True)
    worker.start()
    # Wait briefly for export contexts to register.  The snapshot is also saved
    # below, so a scheduler that starts late remains visible in the result.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if DECODE_WORKER_BUDGET.describe()["active"] > 1:
            break
        if not worker.is_alive():
            break
        time.sleep(0.005)

    latencies: list[float] = []
    snapshots: list[dict] = []
    start = time.perf_counter()
    try:
        source.seek_frame(0)
        for expected in range(total):
            t0 = time.perf_counter()
            frame = source.read_next_frame()
            elapsed = (time.perf_counter() - t0) * 1000.0
            if frame.frame_index != expected:
                raise RuntimeError(
                    f"顺序帧号错误：expected={expected}, got={frame.frame_index}"
                )
            latencies.append(elapsed)
            snapshots.append(DECODE_WORKER_BUDGET.describe())
            del frame
    finally:
        source.close()
    worker.join(timeout=10.0)
    if worker.is_alive():
        raise RuntimeError("export worker 未在 10 秒内结束")
    if export_error:
        raise export_error[0]

    stats = latency_stats(latencies)
    result = {
        "manifest": manifest,
        "scope": "ToposMediaSource CPU planar delivery while parallel export decoder runs",
        "input": {
            "path": str(input_path),
            "sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
            "frames": total,
        },
        "config": {
            "threads": args.threads,
            "export_workers": args.export_workers,
            "decoder_workers": args.decoder_workers,
        },
        "playback": stats,
        "budget_snapshots": snapshots,
        "budget_peak": max(
            snapshots,
            key=lambda item: item["assigned_total"],
            default=DECODE_WORKER_BUDGET.describe(),
        ),
        "wall_ms": (time.perf_counter() - start) * 1000.0,
    }
    manifest_json = json.dumps(manifest, sort_keys=True).encode()
    manifest_sha8 = hashlib.sha256(manifest_json).hexdigest()[:8]
    utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    out_dir = (PROJECT_ROOT / "bench_out" / "d3_budget" / manifest["git_head"]
               / manifest["dylib_sha256"][:8] / manifest_sha8 / utc)
    out_dir.mkdir(parents=True, exist_ok=False)
    out_file = out_dir / "result.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print("[manifest]", json.dumps({k: manifest[k] for k in (
        "git_head", "git_dirty", "dylib_sha256", "slice_threads")},
        ensure_ascii=False))
    print("[playback]", "frames=%d p50=%.3fms p95=%.3fms p99=%.3fms fps=%.1f" % (
        stats["count"], stats["p50_ms"], stats["p95_ms"], stats["p99_ms"], stats["fps"]))
    print("[budget_peak]", json.dumps(result["budget_peak"], ensure_ascii=False))
    print(f"[result] {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
