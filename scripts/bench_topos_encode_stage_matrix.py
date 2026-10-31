#!/usr/bin/env python3
"""T-P1-11 step 1 — TOPOS CPU encode stage matrix (native encode_stage_stats).

Task card (docs/ai_executable_optimization_upgrade_plan_2026-09-17.md
§T-P1-11) step 1: build a matrix over output profile (proxy/lt/standard/hq),
source resolution (2K/4K real TOPOS sources), slice thread count and
slice_rows before changing anything, and locate the dominant encode stage.

Every combo runs in an isolated child process with ``TOPOS_CODEC_PROFILE=1``
so the native dev counters are populated.  Each child reuses one real decoded
planar ``yuv422p10le`` frame (same policy as ``bench_topos_anchor_matrix.py
--kind encode_only``) so wall time isolates encoder work, then performs a
native decode roundtrip of its own output and records Y/U/V mean deltas as
the per-combo quality sanity evidence.

Counter semantics (native/topos_codec/src/codec/codec.h M6): ``pad/fill/
entropy/crc/copy/asm/dct/probe_fill`` are worker CPU cumulative sums (task
local timing summed after join — they may exceed wall when slices run in
parallel), while ``prep/probe/final/check_wall_ns`` are wall-clock buckets
that add up to roughly the sized-path total wall.  The report keeps both so
wall and multithreaded cumulative CPU time are never conflated (card step 5).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "native" / "topos_codec" / "build" / "release" / "topos_codec.0.1.0.dylib"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SOURCES = {
    "2K": ROOT / "tests" / "test_videos" / "2K_topos_proxy.mov",
    "4K": ROOT / "tests" / "test_videos" / "4K_topos_proxy.mov",
}
PROFILES = ("proxy", "lt", "standard", "hq")
STAGE_CPU_KEYS = ("pad_ns", "fill_ns", "entropy_ns", "crc_ns", "copy_ns",
                  "asm_ns", "dct_ns", "probe_fill_ns")
STAGE_WALL_KEYS = ("prep_wall_ns", "probe_wall_ns", "final_wall_ns",
                   "check_wall_ns")
DEFAULT_FRAMES = 48
DEFAULT_WARMUP = 8


def _percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * p))))
    return float(ordered[index])


_PLANE_NAMES = ("Y", "U", "V", "A")


def _plane_means(frame: Any) -> dict[str, float]:
    # DecodedFrame.planes is a positional ndarray tuple (Y, U, V, [A]).
    means: dict[str, float] = {}
    for name, plane in zip(_PLANE_NAMES, frame.planes):
        means[name] = round(float(plane.mean()), 3)
    return means


def run_combo(source: Path, profile: str, threads: int, rows: int,
              frames: int, warmup: int, output: Path) -> dict[str, Any]:
    from src.shared.codec.topos_binding import ToposCodec
    from src.shared.export.topos_encoder import ToposVideoEncoder
    from src.shared.media.metadata import Rational
    from src.shared.media.topos_source import ToposMediaSource

    source_handle = ToposMediaSource(str(source), decode_role="export")
    source_handle.open()
    try:
        meta = source_handle.probe()
        frame = source_handle.read_frame(0)
        input_means = _plane_means(frame)
        config = _encoder_config(meta, profile)
        encoder = ToposVideoEncoder(str(output), config,
                                    slice_threads=threads, slice_rows=rows)
        try:
            encoder.open()
            for _ in range(warmup):
                encoder.encode_frame(frame)
            codec = ToposCodec()
            codec.encode_stage_stats_reset()
            samples: list[float] = []
            started = time.perf_counter()
            for _ in range(frames):
                tick = time.perf_counter()
                encoder.encode_frame(frame)
                samples.append((time.perf_counter() - tick) * 1000.0)
            encoder.close()
            wall_s = time.perf_counter() - started
        finally:
            if encoder.is_open:
                try:
                    encoder.abort()
                except Exception:
                    pass
        stage = _native_encode_stage_stats(codec)
    finally:
        source_handle.close()

    roundtrip = _roundtrip_check(output, frame)
    output_bytes = output.stat().st_size if output.exists() else 0
    result: dict[str, Any] = {
        "status": "ok" if output.exists() and roundtrip["status"] == "ok" else "failed",
        "source": source.name,
        "profile": profile,
        "slice_threads": threads,
        "slice_rows": rows,
        "frames": frames,
        "warmup": warmup,
        "wall_s": wall_s,
        "fps": frames / wall_s if wall_s else 0.0,
        "frame_ms_p50": statistics.median(samples),
        "frame_ms_p95": _percentile(samples, 0.95),
        "bytes_per_frame": output_bytes / frames if frames else 0,
        "output_bytes": output_bytes,
        "input_plane_means": input_means,
        "roundtrip": roundtrip,
        "stage": stage,
    }
    output.unlink(missing_ok=True)
    return result


def _encoder_config(meta: dict[str, Any], profile: str) -> Any:
    from src.shared.export.video_encoder import FFmpegEncoderConfig
    from src.shared.media.metadata import Rational

    return FFmpegEncoderConfig(
        codec="topos",
        width=int(meta.width),
        height=int(meta.height),
        fps=Rational(int(meta.fps.num), int(meta.fps.den)),
        pix_fmt="yuv422p10le",
        profile=profile,
        crf=20,
        threads=0,
        pipelined_conversion=False,
        color_metadata=None,
    )


def _native_encode_stage_stats(codec: Any) -> dict[str, Any]:
    try:
        stats = codec.encode_stage_stats_get()
        names = STAGE_CPU_KEYS + STAGE_WALL_KEYS + (
            "slices", "blocks", "probe_wall_ns")
        return {name: int(getattr(stats, name)) for name in set(names)}
    except Exception as exc:  # old native libraries may not expose stats
        return {"unavailable": f"{type(exc).__name__}: {exc}"}


def _roundtrip_check(output: Path, reference_frame: Any) -> dict[str, Any]:
    """Native-decode frame 0 of the produced file and compare plane means."""
    from src.shared.media.topos_source import ToposMediaSource

    if not output.exists():
        return {"status": "no_output"}
    handle = ToposMediaSource(str(output), decode_role="export")
    try:
        handle.open()
        decoded = handle.read_frame(0)
        planes: dict[str, float] = {}
        max_abs = 0.0
        for name, plane in zip(_PLANE_NAMES, decoded.planes):
            ref_plane = reference_frame.planes[len(planes)] \
                if len(planes) < len(reference_frame.planes) else None
            if ref_plane is None:
                continue
            delta = abs(float(plane.mean()) - float(ref_plane.mean()))
            max_abs = max(max_abs, delta)
            planes[name] = round(delta, 3)
        return {"status": "ok", "plane_mean_abs_delta": planes,
                "max_abs_delta": round(max_abs, 3)}
    except Exception as exc:
        return {"status": "exception",
                "exception": f"{type(exc).__name__}: {exc}"}
    finally:
        handle.close()


def _dominant_stage(stage: dict[str, Any]) -> dict[str, Any]:
    cpu_total = sum(float(stage.get(key, 0) or 0) for key in STAGE_CPU_KEYS)
    if cpu_total <= 0:
        return {"stage": "unknown", "share": 0.0}
    name, value = max(
        ((key, float(stage.get(key, 0) or 0)) for key in STAGE_CPU_KEYS),
        key=lambda item: item[1])
    return {"stage": name.removesuffix("_ns"),
            "share": round(value / cpu_total, 4),
            "cpu_total_ns": int(cpu_total)}


def _child(args: argparse.Namespace) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("TOPOS_CODEC_PROFILE", "1")
    os.environ.setdefault("TOPOS_CODEC_LIB", str(LIB))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    result = run_combo(Path(args.source), args.profile, args.threads,
                       args.rows, args.frames, args.warmup, output)
    result["kind"] = "encode_stage_combo"
    Path(args.emit_json).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") == "ok" else 1


def _run_child(spec: dict[str, Any], workdir: Path,
               timeout: float) -> dict[str, Any]:
    tag = f"{spec['res']}-{spec['profile']}-t{spec['threads']}-r{spec['rows']}"
    result_path = workdir / f"child_{tag}.json"
    output_path = workdir / f"child_{tag}.mov"
    cmd = [
        sys.executable, str(Path(__file__).resolve()), "--child",
        "--source", str(SOURCES[spec["res"]]),
        "--profile", spec["profile"],
        "--threads", str(spec["threads"]),
        "--rows", str(spec["rows"]),
        "--frames", str(spec["frames"]),
        "--warmup", str(spec["warmup"]),
        "--output", str(output_path),
        "--emit-json", str(result_path),
    ]
    env = dict(os.environ)
    env.setdefault("TOPOS_CODEC_LIB", str(LIB))
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True,
                              text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "spec": spec, "timeout_s": timeout}
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))
        output_path.unlink(missing_ok=True)
    else:
        result = {"status": "child_no_result",
                  "stdout_tail": proc.stdout[-2000:],
                  "stderr_tail": proc.stderr[-4000:]}
    result["spec"] = spec
    if proc.returncode != 0:
        result.setdefault("child_returncode", proc.returncode)
    return result


def _build_specs(args: argparse.Namespace) -> list[dict[str, Any]]:
    cpu = min(16, os.cpu_count() or 1)
    thread_choices = sorted({1, 4, 8, cpu})
    specs: list[dict[str, Any]] = []
    for res in ("2K", "4K"):
        for profile in PROFILES:
            for threads in thread_choices:
                specs.append({"res": res, "profile": profile,
                              "threads": threads, "rows": 16,
                              "frames": args.frames, "warmup": args.warmup})
    # slice_rows axis at the default production thread budget.
    for res in ("2K", "4K"):
        for rows in (8, 32):
            specs.append({"res": res, "profile": "standard", "threads": cpu,
                          "rows": rows, "frames": args.frames,
                          "warmup": args.warmup})
    return specs


def _parent(args: argparse.Namespace) -> int:
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    specs = _build_specs(args)
    combos: list[dict[str, Any]] = []
    for index, spec in enumerate(specs, start=1):
        tag = f"{spec['res']}-{spec['profile']}-t{spec['threads']}-r{spec['rows']}"
        print(f"[{index}/{len(specs)}] {tag} ...", flush=True)
        result = _run_child(spec, workdir, timeout=args.timeout)
        result["dominant"] = _dominant_stage(result.get("stage", {})
                                             if isinstance(result.get("stage"), dict)
                                             else {})
        combos.append(result)
        print(f"    fps={result.get('fps')} dominant={result['dominant']}",
              flush=True)

    ok = [row for row in combos if row.get("status") == "ok"]
    by_res: dict[str, dict[str, Any]] = {}
    for res in ("2K", "4K"):
        rows = [row for row in ok if row["spec"]["res"] == res]
        if not rows:
            continue
        stage_cpu: dict[str, float] = {key: 0.0 for key in STAGE_CPU_KEYS}
        for row in rows:
            stage = row.get("stage") or {}
            for key in STAGE_CPU_KEYS:
                stage_cpu[key] += float(stage.get(key, 0) or 0)
        total = sum(stage_cpu.values())
        by_res[res] = {
            "combos": len(rows),
            "fps_range": [round(min(r["fps"] for r in rows), 2),
                          round(max(r["fps"] for r in rows), 2)],
            "stage_cpu_share": {key: round(value / total, 4)
                                for key, value in stage_cpu.items()} if total else {},
        }
    report = {
        "task": "T-P1-11 step1 encode stage matrix",
        "started_at": started_at,
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "frames_per_combo": args.frames,
        "warmup_per_combo": args.warmup,
        "crf": 20,
        "pix_fmt": "yuv422p10le",
        "counter_semantics": {
            "worker_cpu_cumulative": list(STAGE_CPU_KEYS),
            "wall_buckets": list(STAGE_WALL_KEYS),
            "note": "worker CPU sums may exceed wall when slices run parallel",
        },
        "summary_by_resolution": by_res,
        "combos": combos,
    }
    out_json = workdir / "result.json"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"wrote {out_json}")
    return 0 if ok else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--source")
    parser.add_argument("--profile", default="proxy")
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--rows", type=int, default=16)
    parser.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    parser.add_argument("--output", default="bench_out/tmp_tp111.mov")
    parser.add_argument("--emit-json", default="bench_out/tmp_tp111.json")
    parser.add_argument("--workdir",
                        default="bench_out/ai_upgrade/T-P1-11_stage_matrix")
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args(argv)
    if args.child:
        return _child(args)
    return _parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
