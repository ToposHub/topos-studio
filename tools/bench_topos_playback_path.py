#!/usr/bin/env python3
"""ToposMediaSource 播放路径基准（B0/D1）。

边界是 ToposMediaSource 的 ``read_next_frame``/``read_frame``：包含 MOV
packet 读取、Topos decoder context、worker pool、读前批量和 numpy 平面交付，
不包含 GPU 上传、调色、合成、present 或 Qt 调度。结果写入带 manifest 的
不可覆盖目录，避免把 codec 数字误标成完整播放性能。

``--cold-start N``（BENCH-0001）：N 次**全新进程**冷启动首帧计时。每次
采样 spawn 一个子进程（``--cold-start-probe`` 内部模式）执行
open() → seek(0) → read_next_frame()，dylib/平面池/context 全部冷加载；
子进程把计时 JSON 打到 stdout，父进程聚合写入 result 的 ``cold_start`` 段。

result.json schema（P0-04 树 bench_out/d1_playback/<head>/<dylib8>/
<manifest8>/<utc>/result.json）::

    {
      "manifest":   {...},            # bench_manifest.v1（git/dylib SHA 锚点）
      "scope":      {"boundary", "excludes"},
      "input":      {"path", "sha256", "width", "height", "fps",
                     "total_frames", "measured_frames",
                     "pixel_format", "bit_depth"},
      "config":     {"threads", "decoder_workers_requested",
                     "read_ahead_requested", "repeats", "random_seeks",
                     "cold_start"},        # cold_start = 请求的 N（0=未测）
      "cold_start": {                     # 仅 --cold-start N>0 时存在
        "count": N,
        "method": "fresh subprocess per sample (--cold-start-probe child)",
        "cold_first_frame_ms": [样本×N],  # 冷启动 open()+首帧总耗时
        "open_ms": [样本×N],              # open() 自身（moov 解析/context）
        "p50_ms", "p95_ms", "min_ms", "max_ms", "mean_ms": float,
                                        # 上述基于 cold_first_frame_ms 样本
        "probe_argv": [...]              # 子进程命令模板（复现用）
      },
      "diagnostics_after_open": {...},
      "sequential_runs": [{"count", "mean_ms", "p50_ms", "p95_ms",
                           "p99_ms", "fps", "wall_ms",
                           "first_frame", "last_frame"}],
      "random_seek": {"..."同上, "indices"} | null,
      "rss_peak_mib": float
    }

示例：
    TOPOS_CODEC_LIB=... .venv/bin/python tools/bench_topos_playback_path.py \
        --input tests/test_videos/2K_topos.mov --frames 120 --repeats 5 \
        --threads 4 --allow-dirty
    # BENCH-0001 冷启动首帧（≥30 样本建议）：
    ... --cold-start 30
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = Path(__file__).resolve().parent
for path in (PROJECT_ROOT, TOOLS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from bench_manifest import ManifestMismatch, build_manifest  # noqa: E402


def percentile(values: list[float], p: float) -> float:
    """线性插值百分位；空样本返回 0。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * p
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def latency_stats(values: list[float]) -> dict[str, float | int]:
    """返回帧级 p50/p95/p99、均值和吞吐（单位 ms/fps）。"""
    if not values:
        return {"count": 0, "mean_ms": 0.0, "p50_ms": 0.0,
                "p95_ms": 0.0, "p99_ms": 0.0, "fps": 0.0}
    total_s = sum(values) / 1000.0
    return {
        "count": len(values),
        "mean_ms": statistics.mean(values),
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
        "fps": len(values) / total_s if total_s > 0.0 else 0.0,
    }


def rss_mib() -> float | None:
    """读取进程峰值 RSS；macOS 的 getrusage 单位是 bytes。"""
    try:
        import resource

        value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        if sys.platform == "darwin":
            return value / (1024.0 * 1024.0)
        return value / 1024.0
    except Exception:
        return None


def run_sequential(source, frames: int) -> dict:
    """从 0 顺序读 frames 帧，释放上一帧以允许平面池滚动复用。"""
    source.seek_frame(0)
    latencies: list[float] = []
    first_frame: int | None = None
    last_frame: int | None = None
    t_all = time.perf_counter()
    for expected in range(frames):
        t0 = time.perf_counter()
        frame = source.read_next_frame()
        elapsed = (time.perf_counter() - t0) * 1000.0
        index = getattr(frame, "frame_index", None)
        if index != expected:
            raise RuntimeError(f"顺序帧号错误：expected={expected}, got={index}")
        if first_frame is None:
            first_frame = int(index)
        last_frame = int(index)
        latencies.append(elapsed)
        # 交付边界是调用方释放上一帧后再取下一帧；避免基准人为阻塞池复用。
        del frame
    total_ms = (time.perf_counter() - t_all) * 1000.0
    result = latency_stats(latencies)
    result["wall_ms"] = total_ms
    result["first_frame"] = first_frame
    result["last_frame"] = last_frame
    return result


def run_random_seek(source, total_frames: int, count: int) -> dict:
    """按固定随机序列 seek+decode，测随机访问而非顺序吞吐。"""
    rng = random.Random(0xD1A6E)
    indices = [rng.randrange(total_frames) for _ in range(count)]
    latencies: list[float] = []
    t_all = time.perf_counter()
    for expected in indices:
        t0 = time.perf_counter()
        frame = source.read_frame(expected)
        elapsed = (time.perf_counter() - t0) * 1000.0
        index = getattr(frame, "frame_index", None)
        if index != expected:
            raise RuntimeError(f"随机帧号错误：expected={expected}, got={index}")
        latencies.append(elapsed)
        del frame
    result = latency_stats(latencies)
    result["wall_ms"] = (time.perf_counter() - t_all) * 1000.0
    result["indices"] = indices
    return result


# ---------------------------------------------------------------------------
# BENCH-0001：全新进程冷启动首帧计时（--cold-start N）


def run_cold_start_probe(args: argparse.Namespace) -> int:
    """冷启动探针子进程（内部模式，由 spawn_cold_starts 拉起）。

    在全新进程内计时 open()（moov 解析/context 建立/dylib 首载）与
    open 后首帧 read_next_frame()，单行 JSON 打到 stdout 供父进程解析。
    """
    from src.shared.codec.topos_binding import ToposCodec
    from src.shared.media.topos_source import ToposMediaSource

    input_path = Path(args.input)
    codec = ToposCodec()
    codec.set_slice_threads(args.threads)
    read_ahead = None if args.read_ahead < 0 else args.read_ahead
    decoder_workers = None if args.decoder_workers < 0 else args.decoder_workers
    t_open0 = time.perf_counter()
    source = ToposMediaSource(
        str(input_path), read_ahead=read_ahead, decode_workers=decoder_workers,
    )
    source.open()
    open_ms = (time.perf_counter() - t_open0) * 1000.0
    frame_index = None
    try:
        t_ff0 = time.perf_counter()
        source.seek_frame(0)
        frame = source.read_next_frame()
        first_frame_ms = (time.perf_counter() - t_ff0) * 1000.0
        index = getattr(frame, "frame_index", None)
        frame_index = int(index) if index is not None else None
        del frame
    finally:
        source.close()
    payload = {
        "open_ms": round(open_ms, 3),
        "first_frame_after_open_ms": round(first_frame_ms, 3),
        "cold_first_frame_ms": round(open_ms + first_frame_ms, 3),
        "frame_index": frame_index,
        "threads": args.threads,
    }
    print("[cold-probe] " + json.dumps(payload, ensure_ascii=False))
    return 0


def spawn_cold_starts(count: int, input_path: Path, threads: int,
                      decoder_workers: int, read_ahead: int) -> dict:
    """spawn N 个全新探针子进程并解析计时样本（父进程侧）。"""
    argv = [
        sys.executable, str(Path(__file__).resolve()),
        "--cold-start-probe",
        "--input", str(input_path),
        "--threads", str(threads),
        "--decoder-workers", str(decoder_workers),
        "--read-ahead", str(read_ahead),
    ]
    samples: list[dict] = []
    for i in range(count):
        proc = subprocess.run(argv, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"冷启动子进程失败（样本 {i + 1}/{count}）："
                f"{proc.stderr[-800:]}")
        payload = None
        for line in proc.stdout.splitlines():
            if line.startswith("[cold-probe] "):
                payload = json.loads(line[len("[cold-probe] "):])
                break
        if payload is None:
            raise RuntimeError(
                f"冷启动子进程（样本 {i + 1}/{count}）未输出探针 JSON："
                f"{proc.stdout[-400:]}")
        samples.append(payload)
    return {"probe_argv": argv, "samples": samples}


def aggregate_cold_start(raw: dict) -> dict:
    """探针样本聚合 → result.json 的 cold_start 段（纯函数，供单测）。"""
    samples = raw["samples"]
    cold = [float(s["cold_first_frame_ms"]) for s in samples]
    opens = [float(s["open_ms"]) for s in samples]
    return {
        "count": len(samples),
        "method": "fresh subprocess per sample (--cold-start-probe child)",
        "cold_first_frame_ms": cold,
        "open_ms": opens,
        "p50_ms": percentile(cold, 0.50),
        "p95_ms": percentile(cold, 0.95),
        "min_ms": min(cold) if cold else 0.0,
        "max_ms": max(cold) if cold else 0.0,
        "mean_ms": statistics.mean(cold) if cold else 0.0,
        "probe_argv": raw["probe_argv"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="Topos MOV 文件")
    ap.add_argument("--frames", type=int, default=120)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--random-seeks", type=int, default=16,
                    help="随机 seek 样本数；0=关闭")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--decoder-workers", type=int, default=-1,
                    help="-1=D3 共享预算，0=进程池默认，正数=每 context 上限")
    ap.add_argument("--read-ahead", type=int, default=-1,
                    help="-1=ToposMediaSource 默认，0=关闭，正数=固定深度")
    ap.add_argument("--cold-start", type=int, default=0, metavar="N",
                    help="N 次全新进程冷启动首帧计时（BENCH-0001；"
                         "0=关闭。首帧口径=open()+首帧 read_next_frame，"
                         "cold_first_frame_ms 写入 result.json；建议 ≥30）")
    ap.add_argument("--cold-start-probe", action="store_true",
                    help=argparse.SUPPRESS)  # 内部模式：探针子进程
    ap.add_argument("--allow-dirty", action="store_true",
                    help="允许工作树有未提交改动；结果标记 git_dirty")
    args = ap.parse_args()
    if args.frames <= 0 or args.repeats <= 0 or args.threads <= 0:
        raise SystemExit("frames/repeats/threads 必须为正数")
    if args.cold_start < 0:
        raise SystemExit("cold-start 必须 >= 0")
    input_path = Path(args.input)
    if not input_path.is_absolute():
        input_path = PROJECT_ROOT / input_path
    if not input_path.is_file():
        raise SystemExit(f"输入不存在：{input_path}")

    if args.cold_start_probe:
        return run_cold_start_probe(args)

    try:
        manifest = build_manifest(allow_dirty=args.allow_dirty)
    except ManifestMismatch as exc:
        print(f"MANIFEST MISMATCH: {exc}", file=sys.stderr)
        return 2

    # 冷启动采样先于父进程自身的解码——每个样本都是全新子进程，
    # 父进程状态不影响子进程的冷加载语义，先测可避免父进程长时运行
    # 的机器噪声窗口与采样窗口交叠。
    cold_start_result = None
    if args.cold_start > 0:
        cold_start_result = aggregate_cold_start(spawn_cold_starts(
            args.cold_start, input_path, args.threads,
            args.decoder_workers, args.read_ahead))
        print("[cold] samples=%d p50=%.3fms p95=%.3fms min=%.3fms max=%.3fms"
              % (cold_start_result["count"], cold_start_result["p50_ms"],
                 cold_start_result["p95_ms"], cold_start_result["min_ms"],
                 cold_start_result["max_ms"]))

    from src.shared.codec.topos_binding import ToposCodec
    from src.shared.media.topos_source import ToposMediaSource

    codec = ToposCodec()
    codec.set_slice_threads(args.threads)
    read_ahead = None if args.read_ahead < 0 else args.read_ahead
    decoder_workers = None if args.decoder_workers < 0 else args.decoder_workers
    source = ToposMediaSource(
        str(input_path), read_ahead=read_ahead, decode_workers=decoder_workers,
    )
    source.open()
    try:
        metadata = source.probe()
        diagnostics = dict(source.diagnostics)
        total_frames = int(source.total_frames)
        frames = min(args.frames, total_frames)
        if frames <= 0:
            raise RuntimeError("输入没有可解码帧")

        # 首帧预热不计入正式样本；随后每轮都从 0 开始，保证 A/B 可复现。
        source.seek_frame(0)
        warm = source.read_next_frame()
        del warm
        sequential_runs = []
        for _ in range(args.repeats):
            sequential_runs.append(run_sequential(source, frames))

        random_result = None
        if args.random_seeks > 0:
            random_result = run_random_seek(
                source, total_frames, min(args.random_seeks, total_frames),
            )
    finally:
        source.close()

    manifest["threads_effective"] = args.threads
    manifest["input_path"] = str(input_path)
    manifest["input_sha256"] = hashlib.sha256(input_path.read_bytes()).hexdigest()
    manifest_json = json.dumps(manifest, sort_keys=True).encode()
    manifest_sha8 = hashlib.sha256(manifest_json).hexdigest()[:8]
    utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    out_dir = (PROJECT_ROOT / "bench_out" / "d1_playback" / manifest["git_head"]
               / manifest["dylib_sha256"][:8] / manifest_sha8 / utc)
    out_dir.mkdir(parents=True, exist_ok=False)

    result = {
        "manifest": manifest,
        "scope": {
            "boundary": "ToposMediaSource read_next_frame/read_frame → CPU planar frame",
            "excludes": ["GPU upload", "color grading", "composition", "present", "Qt scheduling"],
        },
        "input": {
            "path": str(input_path),
            "sha256": manifest["input_sha256"],
            "width": int(metadata.width),
            "height": int(metadata.height),
            "fps": metadata.fps.to_fps(),
            "total_frames": total_frames,
            "measured_frames": frames,
            "pixel_format": metadata.pixel_format,
            "bit_depth": int(metadata.bit_depth),
        },
        "config": {
            "threads": args.threads,
            "decoder_workers_requested": args.decoder_workers,
            "read_ahead_requested": args.read_ahead,
            "repeats": args.repeats,
            "random_seeks": args.random_seeks,
            "cold_start": args.cold_start,
        },
        # BENCH-0001：--cold-start N 时存在（schema 见模块 docstring）；
        # 未请求时为 null，保持既有字段集合稳定。
        "cold_start": cold_start_result,
        "diagnostics_after_open": diagnostics,
        "sequential_runs": sequential_runs,
        "random_seek": random_result,
        "rss_peak_mib": rss_mib(),
    }
    out_file = out_dir / "result.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print("[manifest]", json.dumps({k: manifest[k] for k in (
        "git_head", "git_dirty", "dylib_sha256", "slice_threads")}, ensure_ascii=False))
    print("[path]", diagnostics)
    if cold_start_result is not None:
        print("[cold]", "samples=%d cold_first_frame p50=%.3fms p95=%.3fms "
              "min=%.3fms max=%.3fms" % (
                  cold_start_result["count"], cold_start_result["p50_ms"],
                  cold_start_result["p95_ms"], cold_start_result["min_ms"],
                  cold_start_result["max_ms"]))
    for run in sequential_runs:
        print("[seq]", "frames=%d p50=%.3fms p95=%.3fms p99=%.3fms fps=%.1f" % (
            run["count"], run["p50_ms"], run["p95_ms"], run["p99_ms"], run["fps"]))
    if random_result is not None:
        print("[seek]", "frames=%d p50=%.3fms p95=%.3fms p99=%.3fms" % (
            random_result["count"], random_result["p50_ms"],
            random_result["p95_ms"], random_result["p99_ms"]))
    print(f"[result] {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
