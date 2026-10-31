#!/usr/bin/env python3
"""4K H.264/HEVC 真实素材解码边界基准（BENCH-0009 / playback.md §8.4 缺口 4）。

D1 基准（``bench_out/d1_playback``，tools/bench_topos_playback_path.py）只测
TOPOS 素材；real 系列只有 2K——**4K 互压缩素材（用户最常见的 4K 卡顿场景）
在覆盖目录内无边界数据**。本脚本补齐采集通道：对真实 4K H.264 / HEVC 素材，
在「解码 → CPU 帧」边界（不含 GPU 上传/调色/合成/呈现，与 D1 同边界语义）
测顺序解码与随机 seek 的帧级延迟分布。

两臂（同一素材的两条产品解码路径，像素格式/硬件路径不同，仅作瓶颈定位）：

1. ``ffmpeg``        FFmpegMediaSource（PyAV 软解，GOP 前向追帧）；
2. ``videotoolbox``  MacOSVtZeroCopySource（VideoToolbox 硬解 pixel buffer）。

输出写入
``bench_out/real_4k_intercodec/<git_head>/<dylib8>/<manifest8>/<utc>/result.json``。
每个 ``combos["<codec>/<arm>"]`` 块与 d1_playback result.json **同构**（键
``scope``/``input``/``config``/``diagnostics_after_open``/``sequential_runs``/
``random_seek`` 字段名与统计口径逐一对应；``manifest`` 提升到顶层共享）。

数据纪律（对应报告的 BENCH-0009 判据，违反即结果只能作单次观察）：

- **repeats ≥ 3**：默认 3。``--repeats`` < 3 时结果写入
  ``repeats_warning`` 且 stdout 打 WARNING（44 文件全 0 字段的教训：无重复
  样本的数据无法区分调度噪声与真实尾部）。
- **commit 记录**：结果必须经 ``bench_manifest.build_manifest`` 锚定
  （git head + dylib SHA + ABI + CPU）；dirty 树须显式 ``--allow-dirty``，
  dylib 内嵌 commit 与 HEAD 不一致直接 exit 2（P0-04 断言）。
- p99 对 30fps 帧预算（33.3 ms）的判定留给 ``tools/check_decode_budget.py``
  类门禁，本脚本只采集。

示例：
    TOPOS_CODEC_LIB=... .venv/bin/python tools/bench_real_4k_h264_hevc.py \
        --codecs h264 hevc --frames 96 --repeats 3 --allow-dirty

    # 无媒体环境自检（参数/输出路径校验 + 打印计划，不触碰 dylib）：
    .venv/bin/python tools/bench_real_4k_h264_hevc.py --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
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

DEFAULT_OUT_ROOT = PROJECT_ROOT / "bench_out" / "real_4k_intercodec"

#: codec → 默认素材（playback.md §8.4：真实 4K 互压缩素材）。
DEFAULT_INPUTS = {
    "h264": "tests/test_videos/4K_H264.mov",
    "hevc": "tests/test_videos/4K_H265.mov",
}
ALL_ARMS = ("ffmpeg", "videotoolbox")
#: 与 d1_playback 的 run_random_seek 同种子语义（固定可复现序列）。
SEEK_RNG_SEED = 0xD1A6E


def percentile(values: list[float], p: float) -> float:
    """线性插值百分位；空样本返回 0（与 d1/bench_topos_playback_path 一致）。"""
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


def latency_stats(values: list[float], wall_ms: float) -> dict:
    """帧级延迟样本 → d1 同构统计块（sequential_runs 条目形状）。"""
    total_s = sum(values) / 1000.0
    return {
        "count": len(values),
        "mean_ms": statistics.mean(values) if values else 0.0,
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
        "fps": len(values) / (wall_ms / 1000.0) if wall_ms > 0 else 0.0,
        "fps_by_latency_sum": len(values) / total_s if total_s > 0 else 0.0,
        "wall_ms": wall_ms,
    }


def probe_input(path: Path) -> dict:
    """PyAV 探测输入几何（两臂共用；d1 input 块同名字段）。"""
    import av

    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        rate = stream.average_rate
        fps = float(rate) if rate else 0.0
        # 先解一帧拿实际像素格式（流级 format 可能为 None）
        for frame in container.decode(stream):
            fmt = frame.format.name
            width, height = frame.width, frame.height
            break
        else:  # pragma: no cover - 空流
            raise RuntimeError(f"输入没有可解码帧：{path}")
        total = int(stream.frames or 0)
        if (total <= 0 and container.duration and fps > 0):
            # PyAV 对部分 MP4/MOV 不回填 stream.frames → 用容器时长估算
            total = int(round(float(container.duration) * fps))
    bit_depth = 8
    for token in fmt.replace("le", "").replace("be", "").split("p"):
        if token.isdigit():
            bit_depth = int(token)
            break
    return {"width": width, "height": height, "fps": fps,
            "pixel_format": fmt, "bit_depth": bit_depth,
            "total_frames": total}


def open_arm(arm: str, path: Path):
    """打开一条解码臂，返回 (source, diagnostics_after_open)。"""
    if arm == "ffmpeg":
        from src.shared.media.ffmpeg_source import FFmpegMediaSource

        source = FFmpegMediaSource(str(path))
        source.open()
        diagnostics = dict(getattr(source, "diagnostics", {}) or {})
        diagnostics.setdefault("decode_path", "ffmpeg_pyav_software")
        return source, diagnostics
    if arm == "videotoolbox":
        from src.shared.media.macos_vt.source import MacOSVtZeroCopySource

        source = MacOSVtZeroCopySource()
        if not source.open(str(path)):
            raise RuntimeError(f"VideoToolbox 打开失败：{path}")
        diagnostics = {
            "decode_path": "videotoolbox_hardware",
            "fallback_reason": getattr(source, "_fallback_reason", None),
        }
        return source, diagnostics
    raise ValueError(f"未知臂：{arm}")


def read_arm_frame(source, arm: str, index: int):
    """按臂读一帧（ffmpeg 走 planar 交付；VT 走 pixel buffer）。"""
    if arm == "ffmpeg":
        if hasattr(source, "read_frame_planar"):
            return source.read_frame_planar(index)
        return source.read_frame(index)
    frame = source.read_frame_pixel_buffer(index)
    if frame is None:
        raise RuntimeError(f"VT 第 {index} 帧解码失败")
    return frame


def close_arm(source, arm: str) -> None:
    if arm == "ffmpeg":
        source.close()
    else:
        source.close()


def run_sequential(source, arm: str, frames: int) -> dict:
    """顺序解码一轮（seek 0 → 逐帧读），d1 sequential_runs 条目同构。"""
    if hasattr(source, "seek_frame"):
        source.seek_frame(0)
    latencies: list[float] = []
    first_frame: int | None = None
    last_frame: int | None = None
    t_all = time.perf_counter()
    for expected in range(frames):
        t0 = time.perf_counter()
        frame = read_arm_frame(source, arm, expected)
        latencies.append((time.perf_counter() - t0) * 1000.0)
        index = getattr(frame, "frame_index", None)
        if index is not None:
            if first_frame is None:
                first_frame = int(index)
            last_frame = int(index)
        del frame
    total_ms = (time.perf_counter() - t_all) * 1000.0
    result = latency_stats(latencies, total_ms)
    result["first_frame"] = first_frame
    result["last_frame"] = last_frame
    return result


def run_random_seek(source, arm: str, total_frames: int, count: int) -> dict:
    """固定随机序列 seek+decode（d1 random_seek 块同构，含 indices）。"""
    rng = random.Random(SEEK_RNG_SEED)
    indices = [rng.randrange(total_frames) for _ in range(count)]
    latencies: list[float] = []
    t_all = time.perf_counter()
    for expected in indices:
        t0 = time.perf_counter()
        frame = read_arm_frame(source, arm, expected)
        latencies.append((time.perf_counter() - t0) * 1000.0)
        del frame
    total_ms = (time.perf_counter() - t_all) * 1000.0
    result = latency_stats(latencies, total_ms)
    result["indices"] = indices
    return result


def rss_mib() -> float | None:
    """进程峰值 RSS（macOS getrusage 单位 bytes；与 d1 同口径）。"""
    try:
        import resource

        value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        if sys.platform == "darwin":
            return value / (1024.0 * 1024.0)
        return value / 1024.0
    except Exception:  # noqa: BLE001
        return None


def validate_args(args: argparse.Namespace) -> None:
    """参数自检（--dry-run 与正式运行共用；非法即 SystemExit）。"""
    if args.frames <= 0:
        raise SystemExit("frames 必须为正数")
    if args.repeats <= 0:
        raise SystemExit("repeats 必须为正数")
    if args.warmup < 0:
        raise SystemExit("warmup 必须 >= 0")
    if args.random_seeks < 0:
        raise SystemExit("random-seeks 必须 >= 0")
    unknown_codec = [c for c in args.codecs if c not in DEFAULT_INPUTS]
    if unknown_codec:
        raise SystemExit(f"未知 codec：{unknown_codec}（可选：{list(DEFAULT_INPUTS)}）")
    unknown_arm = [a for a in args.arms if a not in ALL_ARMS]
    if unknown_arm:
        raise SystemExit(f"未知臂：{unknown_arm}（可选：{list(ALL_ARMS)}）")
    if not args.codecs or not args.arms:
        raise SystemExit("codecs/arms 不能为空")


def resolve_inputs(args: argparse.Namespace) -> dict[str, Path]:
    """codec → 绝对输入路径（--input-h264/--input-hevc 覆盖默认）。"""
    override = {"h264": args.input_h264, "hevc": args.input_hevc}
    inputs: dict[str, Path] = {}
    for codec in DEFAULT_INPUTS:
        raw = override.get(codec)
        if raw:
            p = Path(raw)
            inputs[codec] = p if p.is_absolute() else PROJECT_ROOT / p
        else:
            inputs[codec] = PROJECT_ROOT / DEFAULT_INPUTS[codec]
    return inputs


def plan_dict(args: argparse.Namespace) -> dict:
    """构造 dry-run 计划（纯函数，供单测）。"""
    inputs = resolve_inputs(args)
    return {
        "codecs": list(args.codecs),
        "arms": list(args.arms),
        "frames": args.frames,
        "repeats": args.repeats,
        "warmup": args.warmup,
        "random_seeks": args.random_seeks,
        "threads": args.threads,
        "interleave": bool(args.interleave),
        "repeats_ok": args.repeats >= 3,
        "inputs": {c: str(p) for c, p in inputs.items()},
        "inputs_exist": {c: p.is_file() for c, p in inputs.items()},
        "out_root": str(args.out_root),
        "out_dir_template": "<out_root>/<git_head>/<dylib8>/<manifest8>/<utc>",
    }


def print_plan(plan: dict) -> None:
    """打印 dry-run 计划（人类可读）。"""
    print("[plan] 4K H.264/HEVC 真实素材解码边界（dry-run，不加载 dylib、不测量）")
    print("[plan] codecs =", " ".join(plan["codecs"]),
          "| arms =", " ".join(plan["arms"]))
    print("[plan] frames/repeats/warmup =", plan["frames"], "/",
          plan["repeats"], "/", plan["warmup"],
          "| random_seeks =", plan["random_seeks"],
          "| interleave =", plan["interleave"])
    for codec in plan["codecs"]:
        print(f"[plan] input[{codec}] = {plan['inputs'][codec]} "
              f"exists = {plan['inputs_exist'][codec]}")
        if not plan["inputs_exist"][codec]:
            print(f"[plan] WARNING: {codec} 输入不存在，正式运行将失败"
                  "（dry-run 仅告警）")
    print("[plan] out_root =", plan["out_root"])
    print("[plan] out_dir  =", plan["out_dir_template"], "(result.json)")
    if not plan["repeats_ok"]:
        print("[plan] WARNING: repeats < 3 —— 结果只能作单次观察"
              "（BENCH-0009 数据纪律：正式采集必须 repeats>=3）")
    else:
        print("[plan] repeats >= 3（BENCH-0009 数据纪律满足）")
    print("[plan] 参数与输出路径自检通过")


def resolve_out_dir(out_root: Path, manifest: dict) -> Path:
    """P0-04 树输出目录：<head>/<dylib8>/<manifest8>/<utc>。"""
    manifest_json = json.dumps(manifest, sort_keys=True).encode()
    manifest_sha8 = hashlib.sha256(manifest_json).hexdigest()[:8]
    utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return (out_root / manifest["git_head"] / manifest["dylib_sha256"][:8]
            / manifest_sha8 / utc)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="4K H.264/HEVC 真实素材解码边界基准（ffmpeg/VT 两臂，"
                    "d1 同构输出；BENCH-0009）")
    ap.add_argument("--codecs", nargs="+", default=["h264", "hevc"],
                    choices=list(DEFAULT_INPUTS),
                    help="要测的 codec（默认 h264+hevc）")
    ap.add_argument("--arms", nargs="+", default=list(ALL_ARMS),
                    choices=list(ALL_ARMS),
                    help="解码臂（默认全部两臂）")
    ap.add_argument("--input-h264", default="",
                    help="H.264 素材覆盖（默认 tests/test_videos/4K_H264.mov）")
    ap.add_argument("--input-hevc", default="",
                    help="HEVC 素材覆盖（默认 tests/test_videos/4K_H265.mov）")
    ap.add_argument("--frames", type=int, default=96,
                    help="每轮计帧数（默认 96，与 D1 口径一致）")
    ap.add_argument("--repeats", type=int, default=3,
                    help="重复轮数（默认 3；BENCH-0009 纪律：正式采集"
                         "必须 >= 3，< 3 会写入 repeats_warning）")
    ap.add_argument("--warmup", type=int, default=0,
                    help="每轮丢弃的前 N 帧样本（默认 0=全样本，D1 同口径）")
    ap.add_argument("--random-seeks", type=int, default=16,
                    help="随机 seek 样本数（默认 16，D1 同口径；0=关闭）")
    ap.add_argument("--threads", type=int, default=16,
                    help="TOPOS_CODEC_LIB 侧线程参数记录（默认 16，"
                         "与 D1 manifest 对齐；互压缩臂自身不受此值控制）")
    ap.add_argument("--interleave", action="store_true",
                    help="轮间交替 codec×arm（降低机器漂移对臂间差的影响）")
    ap.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT),
                    help="输出根目录（默认 bench_out/real_4k_intercodec）")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="允许工作树有未提交改动；结果标记 git_dirty")
    ap.add_argument("--dry-run", action="store_true",
                    help="自检参数与输出路径并打印计划后退出（无需媒体/"
                         "dylib，退出码 0）")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    validate_args(args)
    inputs = resolve_inputs(args)

    if args.dry_run:
        print_plan(plan_dict(args))
        return 0

    selected = {c: inputs[c] for c in args.codecs}
    missing = [str(p) for p in selected.values() if not p.is_file()]
    if missing:
        raise SystemExit("输入不存在：" + ", ".join(missing))

    try:
        manifest = build_manifest(allow_dirty=args.allow_dirty)
    except ManifestMismatch as exc:
        print(f"MANIFEST MISMATCH: {exc}", file=sys.stderr)
        return 2
    manifest["threads_effective"] = args.threads

    repeats_warning = None
    if args.repeats < 3:
        repeats_warning = (f"repeats={args.repeats} < 3：结果只能作单次观察，"
                           "不得进入 p99 尾部/相对结论（BENCH-0009）")
        print(f"[WARNING] {repeats_warning}")

    combos: dict[str, dict] = {}
    combo_errors: dict[str, str] = {}
    schedule = [(codec, arm) for codec in args.codecs for arm in args.arms]
    rounds = range(args.repeats)
    ordered = ([(rep, codec, arm) for rep in rounds for codec, arm in schedule]
               if args.interleave else
               [(rep, codec, arm) for codec, arm in schedule for rep in rounds])

    for rep, codec, arm in ordered:
        key = f"{codec}/{arm}"
        path = selected[codec]
        try:
            block = combos.setdefault(key, {
                "input": None, "config": {}, "diagnostics_after_open": None,
                "sequential_runs": [], "random_seek": None,
            })
            if block["input"] is None:
                geo = probe_input(path)
                block["input"] = {
                    "path": str(path),
                    "sha256": sha256_file(path),
                    **geo,
                    "measured_frames": min(args.frames,
                                           geo["total_frames"] or args.frames),
                }
                block["config"] = {
                    "frames": block["input"]["measured_frames"],
                    "repeats": args.repeats,
                    "warmup": args.warmup,
                    "random_seeks": args.random_seeks,
                    "interleave": args.interleave,
                }
            source, diagnostics = open_arm(arm, path)
            if block["diagnostics_after_open"] is None:
                block["diagnostics_after_open"] = diagnostics
            try:
                frames = block["input"]["measured_frames"]
                for _ in range(args.warmup):
                    read_arm_frame(source, arm, 0)
                run = run_sequential(source, arm, frames)
                run["repeat"] = rep
                block["sequential_runs"].append(run)
                if (args.random_seeks > 0 and block["random_seek"] is None
                        and rep == 0):
                    total = block["input"]["total_frames"] or frames
                    block["random_seek"] = run_random_seek(
                        source, arm, total,
                        min(args.random_seeks, total))
            finally:
                close_arm(source, arm)
        except Exception as exc:  # 单臂失败不拖垮其余组合，结果内留痕
            combo_errors[key] = f"{type(exc).__name__}: {exc}"
            print(f"[{key}] FAILED rep{rep}: {combo_errors[key]}",
                  file=sys.stderr)
            continue
        last = block["sequential_runs"][-1]
        print(f"[{key}] rep{rep} n={last['count']} p50={last['p50_ms']:.3f}ms "
              f"p95={last['p95_ms']:.3f}ms p99={last['p99_ms']:.3f}ms "
              f"fps={last['fps']:.1f}")

    result = {
        "manifest": manifest,
        "repro": {
            "tool": "tools/bench_real_4k_h264_hevc.py",
            "argv": sys.argv[1:],
        },
        "scope": {
            "boundary": "real-material decode (read frame) → CPU frame",
            "excludes": ["GPU upload", "color grading", "composition",
                         "present", "Qt scheduling"],
            "note": "与 d1_playback 同边界语义；素材为 4K H.264/HEVC 互压缩"
                    "（BENCH-0009 / playback.md §8.4 缺口 4）",
        },
        "config": {
            "codecs": list(args.codecs),
            "arms": list(args.arms),
            "frames": args.frames,
            "repeats": args.repeats,
            "warmup": args.warmup,
            "random_seeks": args.random_seeks,
            "threads": args.threads,
            "interleave": args.interleave,
        },
        "repeats_warning": repeats_warning,
        "combos": combos,
        "combo_errors": combo_errors,
        "rss_peak_mib": rss_mib(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    out_dir = resolve_out_dir(Path(args.out_root), manifest)
    out_dir.mkdir(parents=True, exist_ok=False)
    out_file = out_dir / "result.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print("[manifest]", json.dumps({k: manifest[k] for k in (
        "git_head", "git_dirty", "dylib_sha256", "slice_threads")},
        ensure_ascii=False))
    for key, block in combos.items():
        runs = block["sequential_runs"]
        if runs:
            best = min(r["p99_ms"] for r in runs)
            worst = max(r["p99_ms"] for r in runs)
            print(f"[cmp] {key}: repeats={len(runs)} "
                  f"p99 range {best:.3f}–{worst:.3f} ms")
    print(f"[result] {out_file}")
    if combo_errors:
        return 1 if not combos else 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
