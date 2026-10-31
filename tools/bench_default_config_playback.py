#!/usr/bin/env python3
"""产品默认配置端到端播放基准 + 丢帧/卡顿遥测（BENCH-0007 / BENCH-0008）。

缺口一（BENCH-0007，playback.md §3 注与 §8.2）：D1 播放边界全部跑在
``read_a_head_depth=0``，readahead 收益是另一协议（成对差分稳态吞吐），
两套协议"不可数值复合"——**当前默认配置、真实素材、含丢帧计数**的一次
播放基准缺失。本脚本在同一 harness、同素材、轮内交替地跑两个配置臂：

- ``readahead_auto``：ToposMediaSource 全默认（read_ahead=None → 播放角色
  自动策略，当前代码=关闭；decode_workers=None → D3 共享预算）；
- ``readahead_<N>``：显式深度 N（对应 readahead 证据链的官方窗口配置，
  arena 批量读已并入当前代码，见 REGRESSION_BASELINE ANC-07 注）。

两臂唯一差异是 readahead 深度 → 结果可数值复合，即缺失的"过渡运行"。

缺口二（BENCH-0008，playback.md §2.3 与 §8.3）：docs/4k-playback-
optimization.md §4.2 要求的 ``dropped_frames/late_frames/queue_depth`` 等
字段在覆盖目录内无落盘。本脚本在**解码→CPU planar 帧**边界（D1 同边界，
GPU/呈现侧明确排除）落盘以下 §4.2 对齐字段：

- ``playback_frame_path``（本脚本恒为 decode 边界说明）；
- ``decode_ms avg/p50/p95/p99``（逐帧）；
- ``late_frames``：decode_ms > 帧周期（1000/timeline_fps）的帧数；
- ``dropped_frames``：节拍消费模式下因落后超过一个帧周期而被跳过、
  未被拉取的帧数（``--no-pace`` 时为 null）；
- ``queue_depth``：每次取帧后 readahead 缓存占用采样（深度 0 时恒 0）；
- ``unmeasured_fields``：texture_write_ms/yuv_render_ms/render_submit_ms/
  ui_tick_ms/coalesced_video_commands——GPU/呈现侧，须离屏 GL harness
  （extended_real_tests_20260917 先例）补齐，本脚本如实声明不冒充。

数据纪律：``--repeats`` 默认 3（<3 写 repeats_warning，只能作单次观察）；
manifest 经 bench_manifest 锚定（dirty 须 --allow-dirty）；结果落
``bench_out/default_config_playback/<head>/<dylib8>/<manifest8>/<utc>/result.json``。

示例：
    TOPOS_CODEC_LIB=... .venv/bin/python tools/bench_default_config_playback.py \
        --input tests/test_videos/2K_topos_422_Proxy.mov --frames 240 \
        --repeats 3 --allow-dirty

    # 无媒体环境自检：
    .venv/bin/python tools/bench_default_config_playback.py --dry-run
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
from bench_real_2k_decode_4arm import percentile  # noqa: E402

DEFAULT_OUT_ROOT = PROJECT_ROOT / "bench_out" / "default_config_playback"
DEFAULT_INPUT = PROJECT_ROOT / "tests" / "test_videos" / "2K_topos_422_Proxy.mov"

#: §4.2 要求但本脚本边界（decode→planar）测不到的字段——如实声明。
UNMEASURED_FIELDS = (
    "texture_write_ms", "yuv_render_ms", "render_submit_ms",
    "ui_tick_ms", "coalesced_video_commands",
)


def paced_playback(pull, total: int, period_s: float,
                   clock=time.perf_counter, sleeper=time.sleep) -> dict:
    """节拍消费循环（纯逻辑，时钟可注入供单测）。

    消费者按 ``start + (i+1)*period`` 的槽位呈现第 i 帧：

    - 落后超过一个整周期（now > deadline_i + period，第 i 帧连下一槽都
      赶不上）→ 跳过拉取，计 ``dropped``；
    - 拉取耗时超过 period → 计 ``late``（该帧错过自身槽位）；
    - 提前到位则睡到槽位对齐（模拟 25/30fps 呈现节拍）。

    ``pull(i)`` 返回本次拉取耗时秒数（由调用方计时）。
    """
    start = clock()
    latencies: list[float] = []
    dropped = 0
    frame = 0
    while frame < total:
        deadline = start + (frame + 1) * period_s
        now = clock()
        if now > deadline + period_s:
            dropped += 1
            frame += 1
            continue
        if now < deadline:
            sleeper(deadline - now)
        latencies.append(pull(frame))
        frame += 1
    return {"count": len(latencies), "dropped_frames": dropped,
            "late_frames": sum(1 for dt in latencies if dt > period_s),
            "latencies_s": latencies}


def open_source(input_path: Path, read_ahead: int | None):
    """按配置臂构造 ToposMediaSource（read_a_head=None 即全默认）。"""
    from src.shared.media.topos_source import ToposMediaSource

    return ToposMediaSource(
        str(input_path),
        read_ahead=read_ahead,
        decode_role="playback",
    )


def run_config_arm(input_path: Path, frames: int, read_ahead: int | None,
                   timeline_fps: float, pace: bool, warmup: int) -> dict:
    """跑一个配置臂一轮：返回逐帧延迟(ms)、遥测与诊断。"""
    source = open_source(input_path, read_ahead)
    source.open()
    # 配置类字段跑前快照（decode_path/深度/worker 分配）；readahead
    # 统计（batch_decodes/cache_hits 等运行值）在 finally 跑后合并。
    diagnostics = dict(source.diagnostics)  # @property（topos_source）
    total = min(frames, int(source.total_frames))
    queue_samples: list[int] = []
    try:
        source.seek_frame(0)
        latencies_ms: list[float] = []

        def pull(_index: int) -> float:
            t0 = time.perf_counter()
            frame = source.read_next_frame()
            dt = time.perf_counter() - t0
            del frame
            latencies_ms.append(dt * 1000.0)
            cache = getattr(source, "_ra_cache", None)
            queue_samples.append(len(cache) if cache is not None else 0)
            return dt

        if pace:
            paced = paced_playback(pull, total, 1.0 / timeline_fps)
            telemetry = {"dropped_frames": paced["dropped_frames"],
                         "late_frames": paced["late_frames"],
                         "paced": True}
        else:
            for index in range(total):
                pull(index)
            budget_ms = 1000.0 / timeline_fps
            telemetry = {
                "dropped_frames": None,
                "late_frames": sum(1 for dt in latencies_ms
                                   if dt > budget_ms),
                "paced": False,
                "note": "--no-pace：吞吐口径，dropped 无定义（late 仍按 "
                        "decode_ms > 帧周期计）",
            }
    finally:
        # 跑后统计合并（键名以 _stats 结尾，避免拼写耦合）
        diagnostics.update({
            k: v for k, v in dict(source.diagnostics).items()
            if k.endswith("_stats")})
        source.close()

    if warmup:
        latencies_ms = latencies_ms[warmup:]
        queue_samples = queue_samples[warmup:]
    total_s = sum(latencies_ms) / 1000.0
    stats = {
        "count": len(latencies_ms),
        "mean_ms": statistics.mean(latencies_ms) if latencies_ms else None,
        "p50_ms": percentile(latencies_ms, 0.50) if latencies_ms else None,
        "p95_ms": percentile(latencies_ms, 0.95) if latencies_ms else None,
        "p99_ms": percentile(latencies_ms, 0.99) if latencies_ms else None,
        "fps": len(latencies_ms) / total_s if total_s > 0 else None,
    }
    telemetry.update({
        "decode_ms": {"avg": stats["mean_ms"], "p50": stats["p50_ms"],
                      "p95": stats["p95_ms"], "p99": stats["p99_ms"]},
        "queue_depth": {
            "samples": len(queue_samples),
            "min": min(queue_samples) if queue_samples else None,
            "median": (statistics.median(queue_samples)
                       if queue_samples else None),
            "max": max(queue_samples) if queue_samples else None,
        },
        "readahead_stats": {k: v for k, v in diagnostics.items()
                          if k.endswith("_stats")},
    })
    return {"stats": stats, "telemetry": telemetry,
            "diagnostics": diagnostics}


def aggregate_runs(runs: list[dict]) -> dict:
    """多轮聚合：fps/p95 取中位数，遥测字段逐轮保留 + 中位汇总。"""
    fps_vals = [r["stats"]["fps"] for r in runs if r["stats"]["fps"]]
    p95_vals = [r["stats"]["p95_ms"] for r in runs if r["stats"]["p95_ms"]]
    dropped = [r["telemetry"]["dropped_frames"] for r in runs
               if r["telemetry"]["dropped_frames"] is not None]
    late = [r["telemetry"]["late_frames"] for r in runs]
    return {
        "runs": runs,
        "repeats": len(runs),
        "fps_median": statistics.median(fps_vals) if fps_vals else None,
        "p95_ms_median": statistics.median(p95_vals) if p95_vals else None,
        "dropped_frames_median": (statistics.median(dropped)
                                  if dropped else None),
        "late_frames_median": statistics.median(late) if late else None,
    }


def config_label(read_ahead: int | None) -> str:
    return ("readahead_auto" if read_ahead is None
            else f"readahead_{read_ahead}")


def parse_depths(spec: str) -> list[int | None]:
    """解析 --readahead-depths：``auto,8`` → [None, 8]（纯函数供单测）。"""
    depths: list[int | None] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if token.lower() == "auto":
            depths.append(None)
        else:
            value = int(token)
            if value < 0:
                raise SystemExit(f"非法 readahead 深度：{token}（auto 或 >=0）")
            depths.append(value)
    if not depths:
        raise SystemExit("readahead-depths 不能为空")
    return depths


def validate_args(args: argparse.Namespace) -> None:
    if args.frames <= 0:
        raise SystemExit("frames 必须为正数")
    if args.repeats <= 0:
        raise SystemExit("repeats 必须为正数")
    if args.warmup < 0 or args.warmup >= args.frames:
        raise SystemExit("warmup 必须满足 0 <= warmup < frames")
    if args.timeline_fps <= 0:
        raise SystemExit("timeline-fps 必须为正数")
    parse_depths(args.readahead_depths)


def plan_dict(args: argparse.Namespace) -> dict:
    """dry-run 计划（纯函数供单测）。"""
    depths = parse_depths(args.readahead_depths)
    return {
        "input": str(args.input),
        "input_exists": Path(args.input).is_file(),
        "frames": args.frames,
        "warmup": args.warmup,
        "repeats": args.repeats,
        "repeats_discipline_ok": args.repeats >= 3,
        "timeline_fps": args.timeline_fps,
        "paced": not args.no_pace,
        "configs": [config_label(d) for d in depths],
        "out_root": str(args.out_root),
        "out_dir_template": "<out_root>/<git_head>/<dylib8>/<manifest8>/<utc>",
    }


def print_plan(plan: dict) -> None:
    print("[plan] 产品默认配置播放基准 + 丢帧遥测（dry-run，不加载 dylib）")
    print("[plan] configs       =", " vs ".join(plan["configs"]),
          "（轮内交替；唯一差异=readahead 深度 → 可数值复合）")
    print("[plan] frames/warmup =", plan["frames"], "/", plan["warmup"])
    print("[plan] repeats       =", plan["repeats"],
          "(>=3 纪律)" if plan["repeats_discipline_ok"]
          else "(<3：仅单次观察)")
    print("[plan] timeline_fps  =", plan["timeline_fps"],
          "paced =", plan["paced"])
    print("[plan] telemetry     = decode_ms/late_frames/dropped_frames/"
          "queue_depth（decode→planar 边界）")
    print("[plan] unmeasured    =", "texture_write/yuv_render/"
          "render_submit/ui_tick（GPU/呈现侧，须离屏 GL harness）")
    print("[plan] input         =", plan["input"],
          "exists =", plan["input_exists"])
    if not plan["input_exists"]:
        print("[plan] WARNING: 输入不存在，正式运行将失败（dry-run 仅告警）")
    print("[plan] out_root      =", plan["out_root"])
    print("[plan] 参数与输出路径自检通过")


def resolve_out_dir(out_root: Path, manifest: dict) -> Path:
    """P0-04 树输出目录：<head>/<dylib8>/<manifest8>/<utc>。"""
    manifest_json = json.dumps(manifest, sort_keys=True).encode()
    manifest_sha8 = hashlib.sha256(manifest_json).hexdigest()[:8]
    utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return (out_root / manifest["git_head"] / manifest["dylib_sha256"][:8]
            / manifest_sha8 / utc)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="产品默认配置端到端播放基准 + §4.2 丢帧遥测"
                    "（BENCH-0007 / BENCH-0008）")
    parser.add_argument("--input", default=str(DEFAULT_INPUT),
                        help="TOPOS MOV 输入（真实转码素材）")
    parser.add_argument("--frames", type=int, default=240,
                        help="每轮计帧数（默认 240 ≈ 25fps×9.6s）")
    parser.add_argument("--warmup", type=int, default=8,
                        help="每轮丢弃前 N 帧样本（默认 8）")
    parser.add_argument("--repeats", type=int, default=3,
                        help="每配置臂轮数（默认 3；正式结论须 >=3）")
    parser.add_argument("--readahead-depths", default="auto,8",
                        help="逗号分隔配置臂（auto=ToposMediaSource 播放"
                             "默认；数字=显式深度，对应 readahead 证据链"
                             "官方窗口）")
    parser.add_argument("--timeline-fps", type=float, default=25.0,
                        help="时间线帧率（late/dropped 判定预算 = "
                             "1000/fps ms）")
    parser.add_argument("--no-pace", action="store_true",
                        help="关闭节拍消费（纯吞吐口径，dropped 无定义）")
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

    depths = parse_depths(args.readahead_depths)
    runs_by_config: dict[str, list[dict]] = {
        config_label(d): [] for d in depths}
    for repeat in range(args.repeats):
        # 轮内交替各配置臂；奇数轮倒序（消除臂序机器漂移偏置）
        order = list(depths) if repeat % 2 == 0 else list(reversed(depths))
        for depth in order:
            label = config_label(depth)
            record = run_config_arm(
                input_path, args.frames, depth, args.timeline_fps,
                not args.no_pace, args.warmup)
            record["repeat"] = repeat
            runs_by_config[label].append(record)
            stats = record["stats"]
            print(f"[rep {repeat}] {label:<15} "
                  f"p50={stats['p50_ms'] and round(stats['p50_ms'], 3)} ms "
                  f"fps={stats['fps'] and round(stats['fps'], 1)} "
                  f"late={record['telemetry']['late_frames']} "
                  f"dropped={record['telemetry']['dropped_frames']}")

    configs_out = {label: aggregate_runs(records)
                   for label, records in runs_by_config.items()}
    result = {
        "schema": "bench_default_config_playback.v1",
        "manifest": manifest,
        "playback_frame_path": (
            "ToposMediaSource.read_next_frame → CPU planar frame"
            "（decode 边界；GPU 上传/调色/合成/呈现排除，D1 同边界）"),
        "unmeasured_fields": list(UNMEASURED_FIELDS),
        "unmeasured_note": (
            "§4.2 的 GPU/呈现侧字段须离屏 GL harness 补齐"
            "（extended_real_tests_20260917 先例）；本脚本不冒充"),
        "config": {
            "input": str(input_path),
            "input_sha256": hashlib.sha256(
                input_path.read_bytes()).hexdigest(),
            "frames": args.frames, "warmup": args.warmup,
            "repeats": args.repeats, "timeline_fps": args.timeline_fps,
            "paced": not args.no_pace,
            "frame_budget_ms": 1000.0 / args.timeline_fps,
        },
        "configs": configs_out,
        "repro": {"argv": sys.argv[1:]},
    }
    if args.repeats < 3:
        result["repeats_warning"] = "repeats < 3：结果只能作单次观察（纪律）"

    out_dir = resolve_out_dir(Path(args.out_root), manifest)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "result.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    print("\n[summary]")
    for label, agg in configs_out.items():
        print(f"  {label:<15} fps 中位 "
              f"{agg['fps_median'] and round(agg['fps_median'], 1)} "
              f"p95 中位 {agg['p95_ms_median'] and round(agg['p95_ms_median'], 3)} ms "
              f"late 中位 {agg['late_frames_median']} "
              f"dropped 中位 {agg['dropped_frames_median']}")
    try:
        shown = out_path.relative_to(PROJECT_ROOT)
    except ValueError:
        shown = out_path
    print(f"[result] {shown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
