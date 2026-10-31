#!/usr/bin/env python3
"""wrapper 绑定层解码开销 A/B 官方复现脚本（BENCH-0003）。

背景（playback.md §6.1）：real_2k_h264_20260907 的 120 帧对照显示
ToposMediaSource（产品包装：context + worker pool + 平面池交付）比
native 直调（ToposMovieFile.packet + ToposDecoder）慢 2.3–2.5×
（135.6 vs 335.6 fps；wrapper p50 6.12 ms vs native 2.68 ms）。同族先例
m11_stage2b 的绑定层修复（exact-copy）拿到 +48~60%——该量级差是播放路径
可见的可优化项，但原始产物无命令行记录、单次无 CI，修复效果无从核验。

本脚本提供**同素材、同线程、成对交替（偶数对 wrapper 先/奇数对 native
先）**的采集通道，输出逐对 p50/fps + 成对 bootstrap 95% CI（统计量
median(wrapper_p50)/median(native_p50)，即"延迟开销倍数"；HISTORICAL
规则 2 形态），并附预注册判据：

- ``overhead_open_2x``：CI 下界 > 2.0——BENCH-0003 的 2×+ 开销缺口仍在
  （优化机会成立，现状即此）；
- ``binding_at_parity``：CI 上界 < 1.2——m11 式绑定修复已兑现（验收线）；
- ``overhead_partial``：其余（部分改善，继续跟踪）；
- pairs < 3 → ``insufficient_pairs``（只能作单次观察）。

可选 ``--max-overhead X``（回归门禁）：CI 下界 > X 时 exit 1——绑定层
劣化不得无声超过既有开销水平。

输出写入
``bench_out/wrapper_binding_overhead/<head>/<dylib8>/<manifest8>/<utc>/result.json``
（bench_manifest.v1 锚定；dirty 树须 --allow-dirty）。

示例：
    TOPOS_CODEC_LIB=... .venv/bin/python tools/bench_wrapper_binding_overhead.py \
        --input-topos tests/test_videos/2K_topos_422_Proxy.mov \
        --pairs 12 --allow-dirty

    # 无媒体环境自检（参数/输出路径校验 + 打印计划，不加载 dylib）：
    .venv/bin/python tools/bench_wrapper_binding_overhead.py --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = Path(__file__).resolve().parent
for _path in (PROJECT_ROOT, TOOLS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from bench_manifest import ManifestMismatch, build_manifest  # noqa: E402
from bench_real_2k_decode_4arm import (  # noqa: E402
    arm_topos_native,
    percentile,
)
from bench_spatial_aq_ab import bootstrap_ratio_ci  # noqa: E402

DEFAULT_OUT_ROOT = PROJECT_ROOT / "bench_out" / "wrapper_binding_overhead"
DEFAULT_INPUT = PROJECT_ROOT / "tests" / "test_videos" / "2K_topos_422_Proxy.mov"


def arm_wrapper_timed(path: Path, frames: int, threads: int,
                      decoder_workers: int, read_ahead: int,
                      warmup: int) -> tuple[list[float], dict]:
    """产品包装臂计时 + 采集 ToposMediaSource 诊断（decode_path 等）。

    返回 (计时的逐帧延迟 ms 列表, diagnostics 字典)。诊断只采一次/轮，
    不进入热路径。
    """
    from src.shared.codec.topos_binding import ToposCodec
    from src.shared.media.topos_source import ToposMediaSource

    codec = ToposCodec()
    codec.set_slice_threads(threads)
    source = ToposMediaSource(
        str(path),
        read_ahead=None if read_ahead < 0 else read_ahead,
        decode_workers=None if decoder_workers < 0 else decoder_workers,
    )
    source.open()
    diagnostics = dict(source.diagnostics)  # @property（topos_source）
    times: list[float] = []
    try:
        total = int(source.total_frames)
        n = min(frames, total)
        source.seek_frame(0)
        for _ in range(n):
            t0 = time.perf_counter()
            frame = source.read_next_frame()
            times.append((time.perf_counter() - t0) * 1000.0)
            del frame
    finally:
        source.close()
    return (times[warmup:] if warmup else times), diagnostics


def arm_native_timed(path: Path, frames: int, threads: int,
                     warmup: int) -> list[float]:
    """native 直调臂计时（复用 bench_real_2k_decode_4arm 的臂实现）。"""
    times = arm_topos_native(path, frames, threads)
    return times[warmup:] if warmup else times


def summarize(latencies_ms: list[float]) -> dict:
    """逐帧延迟 → p50/p95/fps 摘要（口径与 d1_playback/4arm 一致）。"""
    if not latencies_ms:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "fps": None}
    total_s = sum(latencies_ms) / 1000.0
    return {
        "count": len(latencies_ms),
        "p50_ms": percentile(latencies_ms, 0.50),
        "p95_ms": percentile(latencies_ms, 0.95),
        "fps": len(latencies_ms) / total_s if total_s > 0 else None,
    }


def verdict_overhead(ci: dict | None, pairs: int,
                     max_overhead: float | None = None) -> dict:
    """预注册判据（纯函数供单测）。

    ``ci`` 为 bootstrap_ratio_ci 输出（ratio=median(wrapper_p50)/
    median(native_p50)，>1 即 wrapper 更慢）。
    """
    result: dict = {"verdict": "insufficient_pairs",
                    "gate": None, "gate_failed": False}
    if pairs < 3:
        result["note"] = "pairs < 3：只能作单次观察，不构成相对结论"
        return result
    if ci is None:
        result["verdict"] = "degenerate"
        result["note"] = "样本退化（<2 对或含非正值），无 CI"
        return result
    low, high = ci["ci_low"], ci["ci_high"]
    if low > 2.0:
        result["verdict"] = "overhead_open_2x"
    elif high < 1.2:
        result["verdict"] = "binding_at_parity"
    else:
        result["verdict"] = "overhead_partial"
    if max_overhead is not None and low > float(max_overhead):
        result["gate"] = float(max_overhead)
        result["gate_failed"] = True
    return result


def plan_dict(args: argparse.Namespace) -> dict:
    """dry-run 计划（纯函数供单测）。"""
    return {
        "input": str(args.input_topos),
        "input_exists": Path(args.input_topos).is_file(),
        "frames": args.frames,
        "warmup": args.warmup,
        "pairs": args.pairs,
        "pairs_discipline_ok": args.pairs >= 3,
        "threads": args.threads,
        "decoder_workers": args.decoder_workers,
        "read_ahead": args.read_ahead,
        "max_overhead": args.max_overhead,
        "out_root": str(args.out_root),
        "out_dir_template": "<out_root>/<git_head>/<dylib8>/<manifest8>/<utc>",
    }


def print_plan(plan: dict) -> None:
    print("[plan] wrapper 绑定层开销 A/B（dry-run，不加载 dylib、不测量）")
    print("[plan] arms          = topos_wrapper vs topos_native（成对交替，"
          "偶对 wrapper 先）")
    print("[plan] frames/warmup =", plan["frames"], "/", plan["warmup"])
    print("[plan] pairs         =", plan["pairs"],
          "(>=3 纪律)" if plan["pairs_discipline_ok"]
          else "(<3：仅单次观察)")
    print("[plan] threads       =", plan["threads"],
          "decoder_workers =", plan["decoder_workers"],
          "read_ahead =", plan["read_ahead"])
    print("[plan] max_overhead  =", plan["max_overhead"])
    print("[plan] input         =", plan["input"],
          "exists =", plan["input_exists"])
    if not plan["input_exists"]:
        print("[plan] WARNING: 输入不存在，正式运行将失败（dry-run 仅告警）")
    print("[plan] out_root      =", plan["out_root"])
    print("[plan] 参数与输出路径自检通过")


def validate_args(args: argparse.Namespace) -> None:
    """参数自检（--dry-run 与正式运行共用）。"""
    if args.frames <= 0:
        raise SystemExit("frames 必须为正数")
    if args.pairs <= 0:
        raise SystemExit("pairs 必须为正数")
    if args.warmup < 0 or args.warmup >= args.frames:
        raise SystemExit("warmup 必须满足 0 <= warmup < frames")
    if args.threads <= 0:
        raise SystemExit("threads 必须为正数")
    if args.max_overhead is not None and args.max_overhead <= 1.0:
        raise SystemExit("max_overhead 必须 > 1.0（开销门禁无意义）")


def resolve_out_dir(out_root: Path, manifest: dict) -> Path:
    """P0-04 树输出目录：<head>/<dylib8>/<manifest8>/<utc>。"""
    manifest_json = json.dumps(manifest, sort_keys=True).encode()
    manifest_sha8 = hashlib.sha256(manifest_json).hexdigest()[:8]
    utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return (out_root / manifest["git_head"] / manifest["dylib_sha256"][:8]
            / manifest_sha8 / utc)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="wrapper 绑定层解码开销成对 A/B + bootstrap CI "
                    "（BENCH-0003）")
    parser.add_argument("--input-topos", default=str(DEFAULT_INPUT),
                        help="TOPOS MOV 输入（wrapper/native 两臂共用）")
    parser.add_argument("--frames", type=int, default=120,
                        help="每臂每轮计帧数（默认 120，与 real_2k 口径一致）")
    parser.add_argument("--warmup", type=int, default=0,
                        help="每轮丢弃的前 N 帧样本（默认 0=全样本计入）")
    parser.add_argument("--pairs", type=int, default=12,
                        help="成对轮数（默认 12；正式结论须 >=3，"
                             "偶对 wrapper 先/奇对 native 先交替）")
    parser.add_argument("--threads", type=int, default=16,
                        help="两臂 slice threads（默认 16）")
    parser.add_argument("--decoder-workers", type=int, default=-1,
                        help="wrapper 臂 decode workers（-1=D3 共享预算）")
    parser.add_argument("--read-ahead", type=int, default=-1,
                        help="wrapper 臂读前批量（-1=播放默认=关闭）")
    parser.add_argument("--max-overhead", type=float, default=None,
                        help="开销回归门禁：CI 下界超过该倍数时 exit 1")
    parser.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT))
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--expect-commit", default=None,
                        help="显式目标 dylib 内嵌 commit（短 SHA；指向非 "
                             "HEAD 构建的库时使用，P0-04 宽松断言）")
    parser.add_argument("--dry-run", action="store_true",
                        help="自检参数并打印计划后退出（无需 dylib/媒体）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    validate_args(args)
    input_path = Path(args.input_topos)
    if not input_path.is_absolute():
        input_path = PROJECT_ROOT / input_path

    if args.dry_run:
        args.input_topos = str(input_path)
        print_plan(plan_dict(args))
        return 0

    if not input_path.is_file():
        raise SystemExit(f"输入不存在：{input_path}")

    try:
        manifest = build_manifest(allow_dirty=args.allow_dirty,
                                  expect_commit=args.expect_commit)
    except ManifestMismatch as exc:
        print(f"MANIFEST MISMATCH: {exc}", file=sys.stderr)
        return 2
    manifest["threads_effective"] = args.threads

    pair_records: list[dict] = []
    first_diagnostics: dict | None = None
    for index in range(args.pairs):
        order = ("topos_wrapper", "topos_native") if index % 2 == 0 else (
            "topos_native", "topos_wrapper")
        round_data: dict = {"pair": index, "order": list(order)}
        for arm in order:
            t0 = time.perf_counter()
            if arm == "topos_wrapper":
                times, diagnostics = arm_wrapper_timed(
                    input_path, args.frames, args.threads,
                    args.decoder_workers, args.read_ahead, args.warmup)
                if first_diagnostics is None:
                    first_diagnostics = diagnostics
            else:
                times = arm_native_timed(
                    input_path, args.frames, args.threads, args.warmup)
            wall = time.perf_counter() - t0
            stats = summarize(times)
            stats["wall_s"] = wall
            round_data[arm] = stats
            print(f"[pair {index:02d}] {arm:<14} "
                  f"p50={stats['p50_ms'] and round(stats['p50_ms'], 3)} ms "
                  f"fps={stats['fps'] and round(stats['fps'], 1)} "
                  f"n={stats['count']}")
        pair_records.append(round_data)

    paired_p50 = [(r["topos_native"]["p50_ms"], r["topos_wrapper"]["p50_ms"])
                  for r in pair_records
                  if r.get("topos_native", {}).get("p50_ms")
                  and r.get("topos_wrapper", {}).get("p50_ms")]
    paired_fps = [(r["topos_wrapper"]["fps"], r["topos_native"]["fps"])
                  for r in pair_records
                  if r.get("topos_native", {}).get("fps")
                  and r.get("topos_wrapper", {}).get("fps")]

    overhead_latency = bootstrap_ratio_ci(paired_p50)  # median(w)/median(n)
    overhead_fps = bootstrap_ratio_ci(paired_fps)      # median(n)/median(w)

    native_p50s = [n for n, _ in paired_p50]
    wrapper_p50s = [w for _, w in paired_p50]
    verdict = verdict_overhead(overhead_latency, len(paired_p50),
                               args.max_overhead)

    result = {
        "schema": "bench_wrapper_binding_overhead.v1",
        "manifest": manifest,
        "config": {
            "input": str(input_path),
            "input_sha256": hashlib.sha256(
                input_path.read_bytes()).hexdigest(),
            "frames": args.frames, "warmup": args.warmup,
            "pairs": args.pairs, "threads": args.threads,
            "decoder_workers": args.decoder_workers,
            "read_ahead": args.read_ahead,
        },
        "wrapper_diagnostics": first_diagnostics,
        "pairs": pair_records,
        "summary": {
            "p50_ms_median_native": (statistics.median(native_p50s)
                                     if native_p50s else None),
            "p50_ms_median_wrapper": (statistics.median(wrapper_p50s)
                                      if wrapper_p50s else None),
            "overhead_latency_ratio_ci95": overhead_latency,
            "overhead_fps_ratio_ci95": overhead_fps,
            "verdict": verdict,
        },
        "baseline_reference": {
            "note": "real_2k_h264_20260907（2026-09-07，无 SHA 锚定）："
                    "wrapper 135.6 vs native 335.6 fps，p50 6.12 vs 2.68 ms"
                    "（playback.md §6.1 / BENCH-0003）",
        },
        "repro": {"argv": sys.argv[1:]},
    }
    if args.pairs < 3:
        result["pairs_warning"] = "pairs < 3：结果只能作单次观察（纪律）"

    out_dir = resolve_out_dir(Path(args.out_root), manifest)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "result.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    print(f"\n[summary] p50 中位 wrapper "
          f"{result['summary']['p50_ms_median_wrapper'] and round(result['summary']['p50_ms_median_wrapper'], 3)} ms"
          f" vs native {result['summary']['p50_ms_median_native'] and round(result['summary']['p50_ms_median_native'], 3)} ms")
    for key in ("overhead_latency_ratio_ci95", "overhead_fps_ratio_ci95"):
        ci = result["summary"][key]
        if ci:
            print(f"[summary] {key}: {ci['ratio_median']:.3f} "
                  f"CI95 [{ci['ci_low']:.3f}, {ci['ci_high']:.3f}]")
    print(f"[verdict] {verdict['verdict']}")
    shown = out_path
    try:
        shown = out_path.relative_to(PROJECT_ROOT)
    except ValueError:
        pass
    print(f"[result ] {shown}")

    if verdict.get("gate_failed"):
        print(f"[gate] 开销 CI 下界超过 max_overhead="
              f"{verdict['gate']}——绑定层回归", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
