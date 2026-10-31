#!/usr/bin/env python3
"""real_2k 4 臂解码对照官方复现脚本（BENCH-0005 / ANC-06）。

复现 ``bench_out/real_2k_h264_20260907/result.json`` 的 ``decode_comparison``
段——原始产物只有数字、没有命令行与脚本（REGRESSION_BASELINE.md ANC-06
标注"建议补基准脚本"）。本脚本把四臂对照参数化并纳入 P0-04 manifest 树：

四臂（同一内容的四种解码路径，像素格式/硬件路径不同，仅作瓶颈定位）：

1. ``ffmpeg``          H.264 源经 FFmpegMediaSource（PyAV 软解）逐帧读；
2. ``videotoolbox``    H.264 源经 MacOSVtZeroCopySource（VideoToolbox 硬解）；
3. ``topos_wrapper``   TOPOS MOV 经 ToposMediaSource（产品包装：context +
   worker pool + 平面池交付）；
4. ``topos_native``    TOPOS MOV 经 ToposMovieFile.packet + ToposDecoder
   直调（绕过包装层，对照 wrapper 开销）。

输出写入
``bench_out/real_2k_4arm/<git_head>/<dylib8>/<manifest8>/<utc>/result.json``，
其中 ``decode_comparison`` 段与既有 real_2k result.json 同构（键名由帧数
参数化，如 ``h264_ffmpeg_120_frames_fps``），并附每臂逐轮明细与复现命令行。

示例：
    TOPOS_CODEC_LIB=... .venv/bin/python tools/bench_real_2k_decode_4arm.py \
        --input-h264 tests/test_videos/2K_h264.mp4 \
        --input-topos tests/test_videos/2K_topos_422_Proxy.mov \
        --frames 120 --repeats 3 --allow-dirty

    # 无媒体环境自检（参数/输出路径校验 + 打印计划，不触碰 dylib）：
    .venv/bin/python tools/bench_real_2k_decode_4arm.py --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
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

DEFAULT_OUT_ROOT = PROJECT_ROOT / "bench_out" / "real_2k_4arm"

# 臂名 → decode_comparison 经典键前缀（与 real_2k_h264_20260907 命名对齐）
CLASSIC_KEY = {
    "ffmpeg": "h264_ffmpeg",
    "videotoolbox": "h264_videotoolbox",
    "topos_wrapper": "topos_wrapper",
    "topos_native": "topos_native",
}
ALL_ARMS = ("ffmpeg", "videotoolbox", "topos_wrapper", "topos_native")


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


def summarize(latencies_ms: list[float], wall_s: float) -> dict:
    """逐帧延迟样本 → 统计块（口径与 d1_playback latency_stats 一致）。"""
    total_s = sum(latencies_ms) / 1000.0
    return {
        "count": len(latencies_ms),
        "mean_ms": statistics.mean(latencies_ms) if latencies_ms else 0.0,
        "p50_ms": percentile(latencies_ms, 0.50),
        "p95_ms": percentile(latencies_ms, 0.95),
        "p99_ms": percentile(latencies_ms, 0.99),
        "fps": len(latencies_ms) / wall_s if wall_s > 0.0 else 0.0,
        "fps_by_latency_sum": (len(latencies_ms) / total_s
                               if total_s > 0.0 else 0.0),
        "wall_ms": wall_s * 1000.0,
    }


# ---------------------------------------------------------------------------
# 四臂实现（每臂返回逐帧延迟 ms 列表；帧数不足时按实际解码帧数计）


def arm_ffmpeg(path: Path, frames: int) -> list[float]:
    """PyAV 软解（FFmpegMediaSource 逐帧顺序读，planar 交付口径）。"""
    from src.shared.media.ffmpeg_source import FFmpegMediaSource

    src = FFmpegMediaSource(str(path))
    src.open()
    times: list[float] = []
    try:
        for i in range(frames):
            t0 = time.perf_counter()
            frame = (src.read_frame_planar(i)
                     if hasattr(src, "read_frame_planar")
                     else src.read_frame(i))
            times.append((time.perf_counter() - t0) * 1000.0)
            del frame
    finally:
        src.close()
    return times


def arm_videotoolbox(path: Path, frames: int) -> list[float]:
    """VideoToolbox 硬解（MacOSVtZeroCopySource pixel-buffer 读取）。"""
    from src.shared.media.macos_vt.source import MacOSVtZeroCopySource

    src = MacOSVtZeroCopySource()
    if not src.open(str(path)):
        raise RuntimeError(f"VideoToolbox 打开失败：{path}")
    times: list[float] = []
    try:
        for i in range(frames):
            t0 = time.perf_counter()
            frame = src.read_frame_pixel_buffer(i)
            if frame is None:
                raise RuntimeError(f"VT 第 {i} 帧解码失败")
            times.append((time.perf_counter() - t0) * 1000.0)
            del frame
    finally:
        src.close()
    return times


def arm_topos_wrapper(path: Path, frames: int, threads: int,
                      decoder_workers: int, read_ahead: int) -> list[float]:
    """产品包装臂（ToposMediaSource：context + worker pool + 平面交付）。"""
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
    return times


def arm_topos_native(path: Path, frames: int, threads: int) -> list[float]:
    """native 直调臂（ToposMovieFile.packet + ToposDecoder，绕过包装层）。"""
    import numpy as np

    from src.shared.codec.topos_binding import (
        ToposCodec,
        ToposDecoder,
        ToposMovieFile,
    )

    codec = ToposCodec()
    codec.set_slice_threads(threads)
    movie = ToposMovieFile(codec, str(path))
    summary = movie.info()
    n = min(frames, int(summary.sample_count))
    first = movie.packet(0)
    dec = ToposDecoder(codec, max_slice_workers=threads)
    try:
        info = dec.prepare(first)
        plane_count = int(info.plane_count)
        planes = []
        for p in range(plane_count):
            pw, ph = codec.plane_geometry(info, p)
            planes.append(np.empty((ph, pw), dtype="<u2"))
        views = dec.make_views(planes)
        times: list[float] = []
        for i in range(n):
            pkt = movie.packet(i)
            t0 = time.perf_counter()
            dec.decode_views(pkt, views, plane_count, len(pkt))
            times.append((time.perf_counter() - t0) * 1000.0)
    finally:
        dec.close()
    return times


def run_arm(arm: str, args: argparse.Namespace, frames_hint: int
            ) -> list[float]:
    """按臂名分发一次运行；frames_hint 为请求帧数。"""
    if arm == "ffmpeg":
        return arm_ffmpeg(args.input_h264, frames_hint)
    if arm == "videotoolbox":
        return arm_videotoolbox(args.input_h264, frames_hint)
    if arm == "topos_wrapper":
        return arm_topos_wrapper(args.input_topos, frames_hint,
                                 args.threads, args.decoder_workers,
                                 args.read_ahead)
    if arm == "topos_native":
        return arm_topos_native(args.input_topos, frames_hint, args.threads)
    raise ValueError(f"未知臂：{arm}")


def classic_comparison(arm_stats: dict[str, dict], frames: int) -> dict:
    """生成与 real_2k_h264_20260907/result.json decode_comparison 同构的段。

    键名规则与原始产物一致：``<arm>_<frames>_frames_fps`` 与 wrapper/native
    的 p50；只包含实际跑成的臂，帧数由参数代入（默认 120 时与原键全同名）。
    """
    out: dict = {}
    for arm, stats in arm_stats.items():
        classic = CLASSIC_KEY.get(arm)
        if classic is None or stats.get("fps_median") is None:
            continue
        out[f"{classic}_{frames}_frames_fps"] = stats["fps_median"]
    for arm in ("topos_wrapper", "topos_native"):
        stats = arm_stats.get(arm)
        if stats and stats.get("p50_ms_median") is not None:
            out[f"{arm}_p50_ms"] = stats["p50_ms_median"]
    return out


def aggregate_arm(runs: list[dict]) -> dict:
    """多轮结果聚合：fps/p50 取中位数，附逐轮明细。"""
    fps_vals = [r["fps"] for r in runs if r.get("count")]
    p50_vals = [r["p50_ms"] for r in runs if r.get("count")]
    return {
        "runs": runs,
        "repeats": len(runs),
        "fps_median": statistics.median(fps_vals) if fps_vals else None,
        "p50_ms_median": statistics.median(p50_vals) if p50_vals else None,
    }


def validate_args(args: argparse.Namespace) -> None:
    """参数自检（--dry-run 与正式运行共用；非法即 SystemExit）。"""
    if args.frames <= 0:
        raise SystemExit("frames 必须为正数")
    if args.repeats <= 0:
        raise SystemExit("repeats 必须为正数")
    if args.warmup < 0 or args.warmup >= args.frames:
        raise SystemExit("warmup 必须满足 0 <= warmup < frames")
    if args.threads <= 0:
        raise SystemExit("threads 必须为正数")
    unknown = [a for a in args.arms if a not in ALL_ARMS]
    if unknown:
        raise SystemExit(f"未知臂：{unknown}（可选：{list(ALL_ARMS)}）")
    if not args.arms:
        raise SystemExit("arms 不能为空")


def plan_dict(args: argparse.Namespace) -> dict:
    """构造 dry-run 计划（纯函数，供单测）。"""
    return {
        "arms": list(args.arms),
        "frames": args.frames,
        "repeats": args.repeats,
        "warmup": args.warmup,
        "interleave": bool(args.interleave),
        "threads": args.threads,
        "decoder_workers": args.decoder_workers,
        "read_ahead": args.read_ahead,
        "input_h264": str(args.input_h264),
        "input_topos": str(args.input_topos),
        "input_h264_exists": Path(args.input_h264).is_file(),
        "input_topos_exists": Path(args.input_topos).is_file(),
        # 正式运行才建 manifest；dry-run 只打印目录模板（无 dylib 依赖）
        "out_dir_template": "<out_root>/<git_head>/<dylib8>/<manifest8>/<utc>",
        "out_root": str(args.out_root),
    }


def print_plan(plan: dict) -> None:
    """打印 dry-run 计划（人类可读）。"""
    print("[plan] real_2k 4 臂解码对照（dry-run，不加载 dylib、不测量）")
    print("[plan] arms          =", " ".join(plan["arms"]))
    print("[plan] frames/repeats/warmup =", plan["frames"], "/",
          plan["repeats"], "/", plan["warmup"])
    print("[plan] interleave    =", plan["interleave"])
    print("[plan] threads       =", plan["threads"],
          "decoder_workers =", plan["decoder_workers"],
          "read_ahead =", plan["read_ahead"])
    print("[plan] input_h264    =", plan["input_h264"],
          "exists =", plan["input_h264_exists"])
    print("[plan] input_topos   =", plan["input_topos"],
          "exists =", plan["input_topos_exists"])
    print("[plan] out_root      =", plan["out_root"])
    print("[plan] out_dir       =", plan["out_dir_template"],
          "(result.json)")
    missing = [k for k in ("input_h264", "input_topos")
               if not plan[f"{k}_exists"]]
    needed = {"input_h264": ("ffmpeg", "videotoolbox"),
              "input_topos": ("topos_wrapper", "topos_native")}
    for k in missing:
        arms = [a for a in plan["arms"] if a in needed[k]]
        if arms:
            print(f"[plan] WARNING: {k} 不存在，臂 {arms} 正式运行将失败"
                  "（dry-run 仅告警）")
    print("[plan] 参数与输出路径自检通过")


def resolve_out_dir(out_root: Path, manifest: dict) -> Path:
    """P0-04 树输出目录：<head>/<dylib8>/<manifest8>/<utc>。"""
    manifest_json = json.dumps(manifest, sort_keys=True).encode()
    manifest_sha8 = hashlib.sha256(manifest_json).hexdigest()[:8]
    utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return (out_root / manifest["git_head"] / manifest["dylib_sha256"][:8]
            / manifest_sha8 / utc)


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="real_2k 4 臂解码对照（ffmpeg/VT/topos wrapper/topos native）"
                    "官方复现脚本（BENCH-0005 / ANC-06）")
    ap.add_argument("--input-h264", default="tests/test_videos/2K_h264.mp4",
                    help="H.264 源（ffmpeg/videotoolbox 两臂输入；相对路径"
                         "按仓库根解析）")
    ap.add_argument("--input-topos",
                    default="tests/test_videos/2K_topos_422_Proxy.mov",
                    help="TOPOS MOV 源（topos_wrapper/topos_native 两臂输入）")
    ap.add_argument("--frames", type=int, default=120,
                    help="每臂计帧数（默认 120，与 real_2k 原口径一致；"
                         "设为素材总帧数即全片口径）")
    ap.add_argument("--repeats", type=int, default=1,
                    help="每臂重复轮数（默认 1；>1 时 fps/p50 取中位数）")
    ap.add_argument("--warmup", type=int, default=0,
                    help="每轮丢弃的前 N 帧样本（默认 0=全样本计入，"
                         "与原 real_2k 口径一致）")
    ap.add_argument("--arms", nargs="+", default=list(ALL_ARMS),
                    choices=list(ALL_ARMS),
                    help="要跑的臂（默认全部四臂）")
    ap.add_argument("--interleave", action="store_true",
                    help="重复轮间交替各臂（降低机器漂移对臂间差的影响）")
    ap.add_argument("--threads", type=int, default=16,
                    help="TOPOS 臂 slice threads（默认 16）")
    ap.add_argument("--decoder-workers", type=int, default=-1,
                    help="wrapper 臂 decode workers（-1=D3 共享预算，"
                         "0=进程池默认，正数=每 context 上限）")
    ap.add_argument("--read-ahead", type=int, default=-1,
                    help="wrapper 臂读前批量（-1=ToposMediaSource 默认，"
                         "0=关闭，正数=固定深度）")
    ap.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT),
                    help="输出根目录（默认 bench_out/real_2k_4arm）")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="允许工作树有未提交改动；结果标记 git_dirty")
    ap.add_argument("--dry-run", action="store_true",
                    help="自检参数与输出路径并打印计划后退出（无需媒体/"
                         "dylib，退出码 0）")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    validate_args(args)
    for attr in ("input_h264", "input_topos"):
        p = Path(getattr(args, attr))
        if not p.is_absolute():
            setattr(args, attr, PROJECT_ROOT / p)

    if args.dry_run:
        print_plan(plan_dict(args))
        return 0

    for attr, arms in (("input_h264", ("ffmpeg", "videotoolbox")),
                       ("input_topos", ("topos_wrapper", "topos_native"))):
        p = getattr(args, attr)
        if not p.is_file() and any(a in args.arms for a in arms):
            raise SystemExit(f"输入不存在：{p}")

    try:
        manifest = build_manifest(allow_dirty=args.allow_dirty)
    except ManifestMismatch as exc:
        print(f"MANIFEST MISMATCH: {exc}", file=sys.stderr)
        return 2
    manifest["threads_effective"] = args.threads

    # 轮序：interleave 逐轮换臂，否则臂主序连跑
    schedule: list[tuple[str, int]] = []
    if args.interleave:
        for r in range(args.repeats):
            schedule.extend((arm, r) for arm in args.arms)
    else:
        for arm in args.arms:
            schedule.extend((arm, r) for r in range(args.repeats))

    arm_runs: dict[str, list[dict]] = {arm: [] for arm in args.arms}
    arm_errors: dict[str, str] = {}
    for arm, repeat in schedule:
        try:
            t_all = time.perf_counter()
            times = run_arm(arm, args, args.frames)
            wall_s = time.perf_counter() - t_all
        except Exception as exc:  # 单臂失败不拖垮其余臂，结果内留痕
            arm_errors[arm] = f"{type(exc).__name__}: {exc}"
            print(f"[{arm}] FAILED: {arm_errors[arm]}", file=sys.stderr)
            continue
        steady = times[args.warmup:]
        stats = summarize(steady, wall_s)
        stats["repeat"] = repeat
        stats["warmup"] = args.warmup
        arm_runs[arm].append(stats)
        print(f"[{arm}] rep{repeat} n={stats['count']} "
              f"p50={stats['p50_ms']:.3f}ms p95={stats['p95_ms']:.3f}ms "
              f"fps={stats['fps']:.1f}")

    arm_stats = {arm: aggregate_arm(runs) for arm, runs in arm_runs.items()}
    comparison = classic_comparison(arm_stats, args.frames)

    result = {
        "manifest": manifest,
        "repro": {
            "tool": "tools/bench_real_2k_decode_4arm.py",
            "argv": sys.argv[1:],
        },
        "config": {
            "frames": args.frames,
            "repeats": args.repeats,
            "warmup": args.warmup,
            "arms": list(args.arms),
            "interleave": args.interleave,
            "threads": args.threads,
            "decoder_workers": args.decoder_workers,
            "read_ahead": args.read_ahead,
        },
        "input": {
            "h264": str(args.input_h264),
            "topos": str(args.input_topos),
        },
        "decode_comparison": comparison,
        "arms": arm_stats,
        "arm_errors": arm_errors,
        "note": "四臂像素格式/硬件路径不同，仅作瓶颈定位（real_2k 自注）",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    out_dir = resolve_out_dir(Path(args.out_root), manifest)
    out_dir.mkdir(parents=True, exist_ok=False)
    out_file = out_dir / "result.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print("[manifest]", json.dumps({k: manifest[k] for k in (
        "git_head", "git_dirty", "dylib_sha256", "slice_threads")},
        ensure_ascii=False))
    for key, value in sorted(comparison.items()):
        print(f"[cmp] {key} = {value}")
    print(f"[result] {out_file}")
    if arm_errors:
        return 1 if not comparison else 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
