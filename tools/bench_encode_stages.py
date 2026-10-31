#!/usr/bin/env python3
"""E1 编码端分层基准。

边界覆盖 ToposVideoEncoder 的输入准备、native 编码、mux 写入和总墙钟，
同时读取 native ``tc_encode_stage_stats`` 与 sized probe 计数。它只回答
“时间花在哪里”，不把 worker CPU 累计误标成单帧延迟，也不改变默认编码策略。

native 阶段计时（BENCH-0010 修复）：native 只在环境变量
``TOPOS_CODEC_PROFILE=1`` 时才累计 ``tc_encode_stage_stats``（首触惰性读
env，见 native/topos_codec/src/codec/codec.c ``tc_profile_enabled``）。
历史 44/44 个 result.json 的 ``native_stage_ms_per_frame`` 全 0 就是跑了
无 profile 的默认环境所致。本脚本现在默认在加载/调用 codec 前设置该环境
变量（``--no-native-profile`` 可关回旧行为），并在结果里记录
``native_profile_env``；若采集后所有计时字段仍为 0（旧 dylib 缺符号或
profile 被显式关闭），则写 ``native_stage_ms_per_frame: null`` 与
``native_timing_available: false``——**不得再写 0 冒充数据**。

口径警告：profile 开启会给 native 热路径加计时钩子，wall/native 段与
09-15~09-27 的无 profile 历史结果**不可直接对账**（历史锚点 ANC-16/ANC-20
引用须注明 profile 状态；做纯 wall 基准时用 ``--no-native-profile``）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = Path(__file__).resolve().parent
for path in (PROJECT_ROOT, TOOLS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from bench_manifest import ManifestMismatch, build_manifest, sha256_file  # noqa: E402
from archive.dbg_encode_stage_probe import _load_frames  # noqa: E402

#: native 阶段统计的计时字段名（_CEncodeStageStats 镜像；见 result 段）。
NATIVE_STAGE_NS_FIELDS = (
    "pad_ns", "fill_ns", "entropy_ns", "crc_ns", "copy_ns", "asm_ns",
    "dct_ns", "probe_fill_ns", "prep_wall_ns", "probe_wall_ns",
    "final_wall_ns", "check_wall_ns",
)


def native_timing_available(stage, fields=NATIVE_STAGE_NS_FIELDS) -> bool:
    """native 阶段计时是否真实采集（任一计时字段 > 0 即视为可用）。

    BENCH-0010：profile 关闭/旧 dylib 缺符号时全字段为 0——此前的脚本把
    全 0 dict 原样写进 result.json，造成 44/44 文件的字段污染。此函数
    供主流程决定写 dict 还是 null + native_timing_available=false。
    """
    return any(int(getattr(stage, name)) > 0 for name in fields)


def _wrap(obj, attr: str, acc: list[int]) -> None:
    original = getattr(obj, attr)

    def timed(*args, **kwargs):
        start = time.perf_counter_ns()
        result = original(*args, **kwargs)
        acc[0] += time.perf_counter_ns() - start
        return result

    setattr(obj, attr, timed)


def _ns_per_frame(value: int, frames: int) -> float:
    return float(value) / 1e6 / max(1, frames)


def _nearest_rank(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def _peak_rss_bytes() -> int | None:
    """进程自启动以来的高水位；包含素材预载，并非仅 native 编码。"""
    try:
        import resource
    except ImportError:
        return None
    peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return peak if sys.platform == "darwin" else peak * 1024


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="E1 编码端分层基准（native 阶段计时默认开启 "
                    "TOPOS_CODEC_PROFILE=1；BENCH-0010）")
    ap.add_argument("--input", required=True)
    ap.add_argument("--frames", type=int, default=16)
    ap.add_argument("--warmup", type=int, default=4)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--qp", type=int, default=None,
                    help="固定 QP；省略则使用当前档位 sized/反馈路径")
    ap.add_argument("--profile", default="hq")
    ap.add_argument("--allow-dirty", action="store_true")
    ap.add_argument("--no-native-profile", action="store_true",
                    help="不设置 TOPOS_CODEC_PROFILE=1（回到旧行为："
                         "native_stage_ms_per_frame 将为 null 而非计时数据；"
                         "纯 wall 口径与历史结果对账时使用）")
    ap.add_argument("--expect-commit", default=None,
                    help="显式目标 dylib 内嵌 commit（短 SHA；指向非 HEAD "
                         "构建的库时使用，P0-04 宽松断言）")
    ap.add_argument("--out-root", default="",
                    help="输出根目录（默认 bench_out/e1_encode；测试/"
                         "验证时重定向到临时目录）")
    ap.add_argument("--dry-run", action="store_true",
                    help="自检参数与输出路径并打印计划后退出（不加载 "
                         "dylib、不测量，退出码 0）")
    return ap


def plan_dict(args: argparse.Namespace) -> dict:
    """dry-run 计划（纯函数，供单测）。"""
    input_path = Path(args.input)
    if not input_path.is_absolute():
        input_path = PROJECT_ROOT / input_path
    out_root = args.out_root or str(PROJECT_ROOT / "bench_out" / "e1_encode")
    return {
        "input": str(input_path),
        "input_exists": input_path.is_file(),
        "frames": args.frames,
        "warmup": args.warmup,
        "threads": args.threads,
        "qp": args.qp,
        "profile": args.profile,
        "native_profile": not args.no_native_profile,
        "expect_commit": args.expect_commit,
        "out_root": out_root,
        "out_dir_template": "<out_root>/<git_head>/<dylib8>/<manifest8>/<utc>",
    }


def print_plan(plan: dict) -> None:
    print("[plan] E1 编码分层基准（dry-run，不加载 dylib、不测量）")
    print(f"[plan] input = {plan['input']} exists = {plan['input_exists']}")
    print(f"[plan] frames/warmup/threads = {plan['frames']} / "
          f"{plan['warmup']} / {plan['threads']}"
          f" | qp = {plan['qp']} | profile = {plan['profile']}")
    print(f"[plan] native_profile(TOPOS_CODEC_PROFILE=1) = "
          f"{plan['native_profile']}"
          "（关闭时 native_stage_ms_per_frame 记 null，BENCH-0010）")
    if not plan["input_exists"]:
        print("[plan] WARNING: 输入不存在，正式运行将失败（dry-run 仅告警）")
    print(f"[plan] out_root = {plan['out_root']}")
    print(f"[plan] out_dir  = {plan['out_dir_template']} (result.json)")
    print("[plan] 参数与输出路径自检通过")


def main() -> int:
    args = build_arg_parser().parse_args()
    if args.frames <= 0 or args.warmup < 0 or args.threads <= 0:
        raise SystemExit("frames/warmup/threads 参数非法")
    if args.qp is not None and not 0 <= args.qp <= 63:
        raise SystemExit("qp 必须在 0..63")
    if args.dry_run:
        print_plan(plan_dict(args))
        return 0
    # BENCH-0010：必须在任何 encode/decode 调用之前设置（native 侧首触
    # 惰性读 env 并缓存；warmup 帧也会触发首次读取）。
    if not args.no_native_profile:
        os.environ.setdefault("TOPOS_CODEC_PROFILE", "1")
    native_profile_env = os.environ.get("TOPOS_CODEC_PROFILE")
    input_path = Path(args.input)
    if not input_path.is_absolute():
        input_path = PROJECT_ROOT / input_path
    if not input_path.is_file():
        raise SystemExit(f"输入不存在：{input_path}")
    try:
        manifest = build_manifest(allow_dirty=args.allow_dirty,
                                  expect_commit=args.expect_commit)
    except ManifestMismatch as exc:
        print(f"MANIFEST MISMATCH: {exc}", file=sys.stderr)
        return 2

    from src.shared.codec.topos_binding import ToposCodec
    from src.shared.export.topos_encoder import ToposVideoEncoder

    frames = _load_frames(str(input_path), args.frames + args.warmup)
    if len(frames) <= args.warmup:
        raise SystemExit("输入帧数不足 warmup + 1")
    first = frames[0]
    width, height = int(first.width), int(first.height)
    pix_fmt = str(first.pixel_format)
    cfg = SimpleNamespace(
        width=width, height=height, fps=Fraction(25, 1),
        pix_fmt=pix_fmt, profile=args.profile, crf=args.qp,
        has_alpha=len(getattr(first, "planes", ())) >= 4,
    )
    mode_tag = "qp" + str(args.qp) if args.qp is not None else "rate"
    out_root = Path(args.out_root) if args.out_root else (
        PROJECT_ROOT / "bench_out" / "e1_encode")
    out = out_root / f"probe_{mode_tag}_{os.getpid()}.mov"
    out.parent.mkdir(parents=True, exist_ok=True)
    codec = ToposCodec()
    codec.set_slice_threads(args.threads)
    encoder = ToposVideoEncoder(str(out), cfg, slice_threads=args.threads)
    encoder.open()
    try:
        for frame in frames[:args.warmup]:
            encoder.encode_frame(frame)
        prep_ns = [0]
        native_ns = [0]
        mux_ns = [0]
        _wrap(encoder, "_prepare_planes", prep_ns)
        _wrap(encoder._codec, "encode_sized", native_ns)
        _wrap(encoder._codec, "encode_frame", native_ns)
        _wrap(encoder._mux, "add_packet", mux_ns)
        codec.encode_stage_stats_reset()
        frame_wall_ms = []
        t0 = time.perf_counter()
        for frame in frames[args.warmup:]:
            frame_t0 = time.perf_counter_ns()
            encoder.encode_frame(frame)
            frame_wall_ms.append((time.perf_counter_ns() - frame_t0) / 1e6)
        wall_s = time.perf_counter() - t0
        stage = codec.encode_stage_stats_get()
        sized_iters = int(encoder._codec._lib.tc_dev_sized_iters())
        sized_calls = int(encoder._codec._lib.tc_dev_sized_calls())
    finally:
        encoder.close()

    measured = len(frames) - args.warmup
    wall_ms = wall_s * 1000.0 / measured
    prep_ms = prep_ns[0] / 1e6 / measured
    native_ms = native_ns[0] / 1e6 / measured
    mux_ms = mux_ns[0] / 1e6 / measured
    # BENCH-0010：native 阶段计时只有在 TOPOS_CODEC_PROFILE=1 下才被累计；
    # 采集不到时写 null + native_timing_available=false，不再写全 0 dict。
    timing_available = native_timing_available(stage)
    if timing_available:
        native_stage_ms = {
            name: _ns_per_frame(getattr(stage, name), measured)
            for name in NATIVE_STAGE_NS_FIELDS
        }
    else:
        native_stage_ms = None
        print("[WARNING] native 阶段计时不可用（TOPOS_CODEC_PROFILE 未开或"
              "旧 dylib 缺符号）：native_stage_ms_per_frame 记 null"
              "（native_timing_available=false，BENCH-0010）")
    result = {
        "manifest": manifest,
        "scope": {
            "boundary": "ToposVideoEncoder input preparation → native encode → mux",
            "excludes": ["source decode/loading", "GPU rendering", "Resolve"],
        },
        "input": {
            "path": str(input_path),
            "sha256": sha256_file(input_path),
            "width": width, "height": height, "pixel_format": pix_fmt,
        },
        "config": {
            "frames": measured, "warmup": args.warmup,
            "threads": args.threads, "qp": args.qp, "profile": args.profile,
        },
        "wall_ms_per_frame": wall_ms,
        "frame_wall_ms": frame_wall_ms,
        "frame_wall_percentile_method": "nearest-rank; includes encode_frame only",
        "frame_wall_p50_ms": _nearest_rank(frame_wall_ms, 0.50),
        "frame_wall_p95_ms": _nearest_rank(frame_wall_ms, 0.95),
        "frame_wall_p99_ms": _nearest_rank(frame_wall_ms, 0.99),
        "process_peak_rss_bytes": _peak_rss_bytes(),
        "process_peak_rss_scope": "entire process including source preload",
        "python_ms_per_frame": prep_ms,
        "native_ms_per_frame": native_ms,
        "mux_ms_per_frame": mux_ms,
        "other_ms_per_frame": wall_ms - prep_ms - native_ms - mux_ms,
        # BENCH-0010：计时段可用性显式化；null = 未采集（不是 0！）。
        "native_timing_available": timing_available,
        "native_profile_env": native_profile_env,
        "native_stage_ms_per_frame": native_stage_ms,
        "native_counts": {"slices": stage.slices, "blocks": stage.blocks},
        "sized": {
            "iters": sized_iters, "calls": sized_calls,
            "iters_per_call": sized_iters / sized_calls if sized_calls else 0.0,
        },
    }
    manifest_json = json.dumps(manifest, sort_keys=True).encode()
    manifest_sha8 = hashlib.sha256(manifest_json).hexdigest()[:8]
    utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    out_dir = (out_root / manifest["git_head"]
               / manifest["dylib_sha256"][:8] / manifest_sha8 / utc)
    out_dir.mkdir(parents=True, exist_ok=False)
    out_file = out_dir / "result.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print("[manifest]", json.dumps({k: manifest[k] for k in (
        "git_head", "git_dirty", "dylib_sha256", "slice_threads")},
        ensure_ascii=False))
    print("[wall] %.3f ms/frame (%.1f fps)" % (wall_ms, 1000.0 / wall_ms))
    print("[split] python=%.3f native=%.3f mux=%.3f other=%.3f ms/frame" % (
        prep_ms, native_ms, mux_ms, result["other_ms_per_frame"]))
    print("[stages]", json.dumps(result["native_stage_ms_per_frame"],
                                 ensure_ascii=False))
    print("[native_timing_available]", timing_available,
          "| TOPOS_CODEC_PROFILE =", native_profile_env)
    print("[sized]", json.dumps(result["sized"], ensure_ascii=False))
    print(f"[result] {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
