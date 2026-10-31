#!/usr/bin/env python3
"""4K XQ 线程轴饱和与"流水线/熵段"转向判定门（BENCH-0014）。

背景（native-threading.md §1.1/§1.3/§4）：i9-9900K 上 4K XQ E1 分段
3 轮细化显示 12→16 线程仅 +1.5%、16T 逐帧 p95 中位 41.63 ms 超 4K25 的
40 ms 帧预算——线程轴近枯竭；报告 §4 的"下一步收益更可能来自 m104 式
流水线化或熵段内核"是【推断】，非数据结论。本脚本把该推断落成**可复跑、
预注册的判定门**，供后续优化决策与验收复用：

同一构建（manifest 锚定）、同素材，轮内交错跑线程序列（默认 8/12/16，
``--threads`` 参数化），每轮记录：

- 逐帧 wall 的 p95（nearest-rank）与均值，E1 分段口径（prep→native→mux，
  排除源解码/GPU/应用调度——与 ANC-16 同边界）；
- native 阶段统计（``TOPOS_CODEC_PROFILE=1``，BENCH-0010 修复口径）中
  **entropy+fill 的 worker 累计份额**（占全部阶段计时字段之和——是"工作
  在哪里"的分配信号，不得当 wall 归因相加，见 bench_encode_stages 口径
  警告）。

预注册判定（``pivot_verdict`` 纯函数供单测）：

1. ``threads_still_profitable``：最高两档线程的 wall 中位收益 ≥
   ``--marginal-threshold``（默认 5%，即 ANC-16 漂移带"12→16 收益应 <5%"
   的反命题）——线程轴还没吃满，先加线程；
2. ``pivot_justified``：线程轴饱和（收益 < 阈值）**且** 最高线程档 p95
   仍超 ``--budget-ms``（默认 40，4K25 帧预算）**且** entropy+fill 份额 ≥
   ``--entropy-floor``（默认 30%）——"收益转向流水线/熵段"由数据成立；
3. ``pivot_target_unclear``：线程饱和、超预算，但 entropy+fill 份额低于
   地板——先重新剖析再选方向；
4. ``budget_met_no_pivot``：线程饱和但 p95 已在预算内——无转向必要；
5. rounds < 2 → ``insufficient_rounds``（单轮只作观察）。

正式结论须 ``--rounds >= 3``（默认 3；<3 写 rounds_warning）；dirty 树须
``--allow-dirty`` 并随结果标注。输出落
``bench_out/thread_pivot_gate/<head>/<dylib8>/<manifest8>/<utc>/result.json``。

示例：
    TOPOS_CODEC_LIB=... .venv/bin/python tools/bench_thread_pivot_gate.py \
        --input tests/test_videos/4K_topos_4444xq.mov --rounds 3 \
        --allow-dirty

    # 无媒体环境自检：
    .venv/bin/python tools/bench_thread_pivot_gate.py --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = Path(__file__).resolve().parent
for _path in (PROJECT_ROOT, TOOLS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from archive.dbg_encode_stage_probe import _load_frames  # noqa: E402
from bench_manifest import ManifestMismatch, build_manifest  # noqa: E402
from bench_encode_stages import (  # noqa: E402
    NATIVE_STAGE_NS_FIELDS,
    _nearest_rank,
    _ns_per_frame,
    native_timing_available,
)

DEFAULT_OUT_ROOT = PROJECT_ROOT / "bench_out" / "thread_pivot_gate"
DEFAULT_INPUT = PROJECT_ROOT / "tests" / "test_videos" / (
    "4K_topos_4444_XQ.mov")
#: 熵段候选的字段（P01 探针的 entropy/fill 累计占比提示）。
ENTROPY_FIELDS = ("entropy_ns", "fill_ns")


def parse_threads(spec: str) -> list[int]:
    """解析线程序列（纯函数供单测）：升序、去重、≥2 档。"""
    values: list[int] = []
    for token in spec.split(","):
        token = token.strip()
        if token:
            values.append(int(token))
    if len(values) < 2:
        raise SystemExit("threads 至少两档（如 8,12,16）")
    if any(v <= 0 for v in values):
        raise SystemExit("threads 必须为正数")
    if len(set(values)) != len(values):
        raise SystemExit("threads 含重复值")
    if values != sorted(values):
        raise SystemExit("threads 必须升序（判定门取最高两档算边际收益）")
    return values


def _wrap(obj, attr: str, acc: list[int]) -> None:
    original = getattr(obj, attr)

    def timed(*args, **kwargs):
        start = time.perf_counter_ns()
        result = original(*args, **kwargs)
        acc[0] += time.perf_counter_ns() - start
        return result

    setattr(obj, attr, timed)


def run_one_round(input_path: Path, threads: int, profile: str,
                  frames_count: int, warmup: int, work_dir: Path) -> dict:
    """跑一轮（单线程档）E1 分段编码；返回逐帧 wall 与阶段统计。"""
    os.environ.setdefault("TOPOS_CODEC_PROFILE", "1")
    from src.shared.codec.topos_binding import ToposCodec
    from src.shared.export.topos_encoder import ToposVideoEncoder

    frames = _load_frames(str(input_path), frames_count + warmup)
    if len(frames) <= warmup:
        raise SystemExit("输入帧数不足 warmup + 1")
    first = frames[0]
    width, height = int(first.width), int(first.height)
    pix_fmt = str(first.pixel_format)
    cfg = SimpleNamespace(
        width=width, height=height, fps=Fraction(25, 1),
        pix_fmt=pix_fmt, profile=profile, crf=None,
        has_alpha=len(getattr(first, "planes", ())) >= 4,
    )
    out = work_dir / f"probe_t{threads}_{os.getpid()}.mov"
    codec = ToposCodec()
    codec.set_slice_threads(threads)
    encoder = ToposVideoEncoder(str(out), cfg, slice_threads=threads)
    encoder.open()
    try:
        for frame in frames[:warmup]:
            encoder.encode_frame(frame)
        prep_ns = [0]
        native_ns = [0]
        mux_ns = [0]
        _wrap(encoder, "_prepare_planes", prep_ns)
        _wrap(encoder._codec, "encode_sized", native_ns)
        _wrap(encoder._codec, "encode_frame", native_ns)
        _wrap(encoder._mux, "add_packet", mux_ns)
        codec.encode_stage_stats_reset()
        walls_ms: list[float] = []
        for frame in frames[warmup:]:
            t0 = time.perf_counter_ns()
            encoder.encode_frame(frame)
            walls_ms.append((time.perf_counter_ns() - t0) / 1e6)
        stage = codec.encode_stage_stats_get()
    finally:
        encoder.close()
    out.unlink(missing_ok=True)

    measured = len(frames) - warmup
    stage_ms = {name: _ns_per_frame(getattr(stage, name), measured)
                for name in NATIVE_STAGE_NS_FIELDS} \
        if native_timing_available(stage) else None
    stage_total_ms = sum(stage_ms.values()) if stage_ms else None
    entropy_ms = (sum(stage_ms[name] for name in ENTROPY_FIELDS)
                  if stage_ms else None)
    return {
        "threads": threads,
        "frames": measured,
        "wall_mean_ms": statistics.mean(walls_ms),
        "wall_p50_ms": _nearest_rank(walls_ms, 0.50),
        "wall_p95_ms": _nearest_rank(walls_ms, 0.95),
        "prep_ms_per_frame": prep_ns[0] / 1e6 / measured,
        "native_ms_per_frame": native_ns[0] / 1e6 / measured,
        "mux_ms_per_frame": mux_ns[0] / 1e6 / measured,
        "native_timing_available": stage_ms is not None,
        "native_stage_ms_per_frame": stage_ms,
        "entropy_fill_ms_per_frame_worker_cumulative": entropy_ms,
        "entropy_fill_share_of_stage_total": (
            entropy_ms / stage_total_ms
            if entropy_ms is not None and stage_total_ms else None),
    }


def aggregate_threads(rounds: list[dict]) -> dict:
    """同线程档多轮聚合：关键量取中位数，轮明细保留。"""
    def median_of(key: str):
        values = [r[key] for r in rounds if r.get(key) is not None]
        return statistics.median(values) if values else None

    return {
        "rounds": rounds,
        "round_count": len(rounds),
        "wall_mean_ms_median": median_of("wall_mean_ms"),
        "wall_p95_ms_median": median_of("wall_p95_ms"),
        "native_ms_per_frame_median": median_of("native_ms_per_frame"),
        "entropy_fill_share_median": median_of(
            "entropy_fill_share_of_stage_total"),
    }


def pivot_verdict(threads_agg: dict[int, dict], budget_ms: float,
                  marginal_threshold_pct: float, entropy_floor: float,
                  round_count: int) -> dict:
    """预注册判定门（纯函数供单测）。

    ``threads_agg`` 键为线程档（升序由调用方保证），值为
    :func:`aggregate_threads` 输出。
    """
    if round_count < 2:
        return {"verdict": "insufficient_rounds",
                "note": "rounds < 2：单轮只作观察，不构成判定"}
    ordered = sorted(threads_agg.items())
    (t_prev, agg_prev), (t_max, agg_max) = ordered[-2], ordered[-1]
    if not agg_prev.get("wall_mean_ms_median") or \
            not agg_max.get("wall_mean_ms_median"):
        return {"verdict": "degenerate",
                "note": "wall 中位缺失（帧数为 0？）"}
    marginal_pct = (1.0 - agg_max["wall_mean_ms_median"]
                    / agg_prev["wall_mean_ms_median"]) * 100.0
    p95_at_max = agg_max.get("wall_p95_ms_median")
    share = agg_max.get("entropy_fill_share_median")
    saturated = marginal_pct < marginal_threshold_pct
    over_budget = p95_at_max is not None and p95_at_max > budget_ms

    result = {
        "marginal_gain_pct": marginal_pct,
        "marginal_pair": [t_prev, t_max],
        "marginal_threshold_pct": marginal_threshold_pct,
        "thread_axis": "saturated" if saturated else "still_scaling",
        "p95_ms_at_max_threads": p95_at_max,
        "budget_ms": budget_ms,
        "over_budget": over_budget,
        "entropy_fill_share_at_max": share,
        "entropy_floor": entropy_floor,
    }
    if not saturated:
        result["verdict"] = "threads_still_profitable"
        result["note"] = (f"{t_prev}→{t_max} wall 收益 "
                          f"{marginal_pct:.1f}% ≥ 阈值——线程轴未枯竭，"
                          "先吃线程再谈转向")
    elif not over_budget:
        result["verdict"] = "budget_met_no_pivot"
        result["note"] = "线程饱和但 p95 已在帧预算内——无转向必要"
    elif share is None:
        result["verdict"] = "pivot_target_unclear"
        result["note"] = ("native 阶段计时不可用（TOPOS_CODEC_PROFILE 未开"
                          "或旧 dylib 缺符号）——无法核验熵段份额")
    elif share >= entropy_floor:
        result["verdict"] = "pivot_justified"
        result["note"] = (f"线程轴饱和（{marginal_pct:.1f}% < "
                          f"{marginal_threshold_pct}%）且 p95 "
                          f"{p95_at_max:.2f} ms 超预算 {budget_ms} ms，"
                          f"entropy+fill 份额 {share:.1%} ≥ "
                          f"{entropy_floor:.0%}——『收益转向流水线/熵段』"
                          "由数据成立（m104 先例：2K +54%/4K +22%）")
    else:
        result["verdict"] = "pivot_target_unclear"
        result["note"] = (f"线程饱和且超预算，但 entropy+fill 份额 "
                          f"{share:.1%} < {entropy_floor:.0%}——先重新"
                          "剖析（prep/mux 或其他段）再选方向")
    return result


def plan_dict(args: argparse.Namespace) -> dict:
    """dry-run 计划（纯函数供单测）。"""
    return {
        "input": str(args.input),
        "input_exists": Path(args.input).is_file(),
        "threads": list(args.threads),
        "rounds": args.rounds,
        "rounds_discipline_ok": args.rounds >= 3,
        "frames": args.frames,
        "warmup": args.warmup,
        "profile": args.profile,
        "budget_ms": args.budget_ms,
        "marginal_threshold_pct": args.marginal_threshold,
        "entropy_floor": args.entropy_floor,
        "out_root": str(args.out_root),
        "out_dir_template": "<out_root>/<git_head>/<dylib8>/<manifest8>/<utc>",
    }


def print_plan(plan: dict) -> None:
    print("[plan] 4K XQ 线程轴饱和与流水线/熵段转向判定门（dry-run，"
          "不加载 dylib）")
    print("[plan] threads        =", plan["threads"],
          "（轮内交错；最高两档算边际收益）")
    print("[plan] rounds         =", plan["rounds"],
          "(>=3 纪律)" if plan["rounds_discipline_ok"]
          else "(<3：仅单次观察)")
    print("[plan] frames/warmup  =", plan["frames"], "/", plan["warmup"],
          "profile =", plan["profile"])
    print("[plan] budget_ms      =", plan["budget_ms"],
          "（4K25 帧预算；超支 + 线程饱和 + 熵段份额达标 → pivot_justified）")
    print("[plan] marginal_thr  =", plan["marginal_threshold_pct"], "%"
          "  entropy_floor =", plan["entropy_floor"])
    print("[plan] input          =", plan["input"],
          "exists =", plan["input_exists"])
    if not plan["input_exists"]:
        print("[plan] WARNING: 输入不存在，正式运行将失败（dry-run 仅告警）")
    print("[plan] out_root       =", plan["out_root"])
    print("[plan] 参数与输出路径自检通过")


def validate_args(args: argparse.Namespace) -> None:
    if args.frames <= 0:
        raise SystemExit("frames 必须为正数")
    if args.warmup < 0 or args.warmup >= args.frames:
        raise SystemExit("warmup 必须满足 0 <= warmup < frames")
    if args.rounds <= 0:
        raise SystemExit("rounds 必须为正数")
    if args.budget_ms <= 0:
        raise SystemExit("budget-ms 必须为正数")
    if not 0.0 < args.marginal_threshold < 100.0:
        raise SystemExit("marginal-threshold 必须在 (0, 100) 开区间")
    if not 0.0 <= args.entropy_floor <= 1.0:
        raise SystemExit("entropy-floor 必须在 [0, 1]（份额）")


def resolve_out_dir(out_root: Path, manifest: dict) -> Path:
    """P0-04 树输出目录：<head>/<dylib8>/<manifest8>/<utc>。"""
    manifest_json = json.dumps(manifest, sort_keys=True).encode()
    manifest_sha8 = hashlib.sha256(manifest_json).hexdigest()[:8]
    utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return (out_root / manifest["git_head"] / manifest["dylib_sha256"][:8]
            / manifest_sha8 / utc)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="4K XQ 线程轴饱和 + 流水线/熵段转向判定门（BENCH-0014）")
    parser.add_argument("--input", default=str(DEFAULT_INPUT),
                        help="4K XQ 素材（Topos MOV）")
    parser.add_argument("--threads", default="8,12,16",
                        help="线程序列（升序，至少两档；默认 8,12,16）")
    parser.add_argument("--rounds", type=int, default=3,
                        help="每线程档轮数（默认 3；正式判定须 >=2，"
                             ">=3 才可作正式结论）")
    parser.add_argument("--frames", type=int, default=16,
                        help="每轮计帧数（默认 16，与试点口径一致）")
    parser.add_argument("--warmup", type=int, default=4,
                        help="每轮预热帧（默认 4，不计样本）")
    parser.add_argument("--profile", default="4444xq",
                        help="编码档位（默认 4444xq）")
    parser.add_argument("--budget-ms", type=float, default=40.0,
                        help="帧预算（默认 40 = 4K25；docs/4k-playback-"
                             "optimization.md §3.1）")
    parser.add_argument("--marginal-threshold", type=float, default=5.0,
                        help="线程轴饱和阈值 %%（默认 5，ANC-16 漂移带"
                             "『12→16 收益应 <5%%』）")
    parser.add_argument("--entropy-floor", type=float, default=0.30,
                        help="entropy+fill 份额地板（默认 0.30）")
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
    args.threads = parse_threads(args.threads)
    validate_args(args)
    input_path = Path(args.input)
    if not input_path.is_absolute():
        input_path = PROJECT_ROOT / input_path

    if args.dry_run:
        args.input = str(input_path)
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
    manifest["threads_list"] = list(args.threads)

    out_dir = resolve_out_dir(Path(args.out_root), manifest)
    work = out_dir / "artifacts"
    work.mkdir(parents=True, exist_ok=True)

    rounds_by_threads: dict[int, list[dict]] = {t: [] for t in args.threads}
    for repeat in range(args.rounds):
        order = list(args.threads) if repeat % 2 == 0 else list(
            reversed(args.threads))
        for threads in order:
            record = run_one_round(input_path, threads, args.profile,
                                   args.frames, args.warmup, work)
            record["repeat"] = repeat
            rounds_by_threads[threads].append(record)
            print(f"[rep {repeat}] t{threads:<3} "
                  f"wall_mean={record['wall_mean_ms']:.2f} ms "
                  f"p95={record['wall_p95_ms']:.2f} ms "
                  f"entropy+fill share="
                  f"{record['entropy_fill_share_of_stage_total'] and round(record['entropy_fill_share_of_stage_total'], 3)}")

    agg = {t: aggregate_threads(records)
           for t, records in rounds_by_threads.items()}
    verdict = pivot_verdict(agg, args.budget_ms, args.marginal_threshold,
                            args.entropy_floor, args.rounds)

    result = {
        "schema": "bench_thread_pivot_gate.v1",
        "manifest": manifest,
        "scope": {
            "boundary": "E1 分段：ToposVideoEncoder prep → native → mux"
                        "（排除源解码/GPU/应用调度；ANC-16 同边界）",
            "stage_stats_caveat": "entropy/fill 为 worker 累计份额"
                                  "（分配信号），不得当 wall 归因相加",
        },
        "config": {
            "input": str(input_path),
            "input_sha256": hashlib.sha256(
                input_path.read_bytes()).hexdigest(),
            "threads": list(args.threads), "rounds": args.rounds,
            "frames": args.frames, "warmup": args.warmup,
            "profile": args.profile, "budget_ms": args.budget_ms,
            "marginal_threshold_pct": args.marginal_threshold,
            "entropy_floor": args.entropy_floor,
            "native_profile_env": os.environ.get("TOPOS_CODEC_PROFILE"),
        },
        "threads": {str(t): a for t, a in agg.items()},
        "verdict": verdict,
        "baseline_reference": {
            "note": "topos_perf_thread_refine_3rounds.json（1d30609f/"
                    "7b0cc4d4，dirty）：12→16 +1.5%、16T p95 41.63 ms；"
                    "m104_pipeline 先例 2K +54%/4K +22%（另一构建，"
                    "只比方向）",
        },
        "repro": {"argv": sys.argv[1:]},
    }
    if args.rounds < 3:
        result["rounds_warning"] = "rounds < 3：判定只能作单次观察（纪律）"

    out_path = out_dir / "result.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    print(f"\n[gate] {verdict['verdict']}")
    for key in ("marginal_gain_pct", "p95_ms_at_max_threads",
                "entropy_fill_share_at_max"):
        if key in verdict:
            print(f"[gate] {key} = {verdict[key]}")
    try:
        shown = out_path.relative_to(PROJECT_ROOT)
    except ValueError:
        shown = out_path
    print(f"[result] {shown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
