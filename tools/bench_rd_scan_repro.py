#!/usr/bin/env python3
"""rd_result 参数化复现脚本（BENCH-0005 / ANC-18）。

复现 ``tools/bench_rd_baseline.py`` 产出的 ``rd_result.json``（历史锚点
``bench_out/6b3dfe9d/…/rd_result.json`` 等）——原始产物未内嵌命令行，
QP 档位/熵模式/分辨率案例须按 manifest 逆推（REGRESSION_BASELINE.md
ANC-18 标注"建议补基准脚本确认"）。本脚本：

1. 复用 ``bench_rd_baseline`` 的口径函数（load_frames/psnr/ssim_y/
   make_config/encode_frames/Decoder/pct/generation_curve），行 schema
   与既有 rd_result.json 逐字段一致（entropy/qp/bytes_per_frame/mbps/
   enc_ms/dec_p50_ms/dec_p95_ms/psnr_y/psnr_u/psnr_v/ssim_y/maxerr）；
2. 把 QP 档位、熵模式（V1/V2 子集）、分辨率案例与自定义素材
   （宽高/profile/pixel_format/bit_depth）、线程数、帧数、代际曲线
   全部参数化；
3. 结果写入 P0-04 manifest 树
   ``bench_out/<git_head>/<dylib8>/<manifest8>/<utc>/rd_result.json``，
   并内嵌 ``repro.argv``——补上"产物未内嵌参数"的流程缺口。

示例：
    # 复现 ANC-18 锚点行（2K V2@qp45 = 245.7 Mbps / PSNR-Y 74.76 dB）：
    TOPOS_CODEC_LIB=... .venv/bin/python tools/bench_rd_scan_repro.py \
        --res 2k --qps 45 --entropy V2 --frames 64 --threads 16

    # 自定义素材与码率档位：
    .venv/bin/python tools/bench_rd_scan_repro.py \
        --input tests/test_videos/2K_prores422HQ.mov \
        --width 1920 --height 1080 --profile 3 --pixel-format 0 \
        --bit-depth 10 --qps 20,25,30,35,40 --entropy V1,V2

    # 无媒体环境自检（参数/输出路径校验 + 打印计划，不触碰 dylib）：
    .venv/bin/python tools/bench_rd_scan_repro.py --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = Path(__file__).resolve().parent
for _path in (PROJECT_ROOT, TOOLS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from bench_manifest import ManifestMismatch, build_manifest  # noqa: E402
from bench_rd_baseline import (  # noqa: E402
    CASES,
    FPS_NOMINAL,
    Decoder,
    encode_frames,
    generation_curve,
    load_frames,
    make_config,
    psnr,
    pct,
    ssim_y,
)

ENTROPY_CODES = {"V1": 0, "V2": 1}


def parse_int_list(text: str, what: str) -> list[int]:
    """逗号分隔整数列表解析（QP 档位等）；空/非法即 SystemExit。"""
    try:
        values = [int(tok) for tok in text.split(",") if tok.strip()]
    except ValueError as exc:
        raise SystemExit(f"--{what} 解析失败（应为逗号分隔整数）：{text!r}") from exc
    if not values:
        raise SystemExit(f"--{what} 不能为空")
    return values


def parse_entropies(text: str) -> list[str]:
    """熵模式列表解析（V1/V2 子集，保序去重）。"""
    tokens = [t.strip().upper() for t in text.split(",") if t.strip()]
    if not tokens:
        raise SystemExit("--entropy 不能为空")
    unknown = [t for t in tokens if t not in ENTROPY_CODES]
    if unknown:
        raise SystemExit(f"未知熵模式：{unknown}（可选 V1/V2）")
    return list(dict.fromkeys(tokens))


def resolve_cases(args: argparse.Namespace) -> dict[str, dict]:
    """把 CLI 参数解析成分辨率案例表 {name: case dict}（纯函数，供单测）。

    --input 自定义案例覆盖 --res 预设；两者至少给一个。
    """
    if args.input:
        case = {
            "src": str(args.input),
            "w": args.width,
            "h": args.height,
            "profile": args.profile,
            "pixel_format": args.pixel_format,
            "bit_depth": args.bit_depth,
        }
        if args.width <= 0 or args.height <= 0:
            raise SystemExit("自定义案例 width/height 必须为正数")
        if args.profile <= 0:
            raise SystemExit("自定义案例 profile 必须为正数")
        if args.pixel_format not in (0, 1, 3):
            raise SystemExit("pixel_format 须为 0(422)/1(444)/3(CFA)")
        if args.bit_depth not in (10, 12, 16):
            raise SystemExit("bit_depth 须为 10/12/16")
        return {"custom": case}
    if not args.res:
        raise SystemExit("须提供 --res 预设或 --input 自定义案例之一")
    unknown = [r for r in args.res if r not in CASES]
    if unknown:
        raise SystemExit(f"未知 --res 预设：{unknown}（可选 {sorted(CASES)}）")
    return {name: dict(CASES[name]) for name in args.res}


def sweep_rows(codec_lib, case: dict, ref_frames: list, qps: list[int],
               entropies: list[str], ssim_every: int
               ) -> list[dict]:
    """按请求的熵模式 × QP 扫描；行 schema 与 rd_result.json 一致。

    与 bench_rd_baseline.rd_sweep 的差别仅在于熵模式列表可参数化
    （原函数固定 V1+V2 全扫）。
    """
    rows: list[dict] = []
    maxv = float((1 << case["bit_depth"]) - 1)
    n = len(ref_frames)
    for tag in entropies:
        entropy = ENTROPY_CODES[tag]
        for qp in qps:
            fc = make_config(codec_lib, case, qp, entropy)
            t0 = time.perf_counter()
            pkts, total = encode_frames(codec_lib, fc, ref_frames)
            enc_ms = (time.perf_counter() - t0) * 1000.0 / n
            with Decoder(codec_lib, pkts[0], case) as dec:
                # 指标必须在逐帧解码循环内即时计算——planes 是复用直写缓冲
                ps = [[], [], []]
                maxerr = 0
                ssims: list[float] = []
                dt: list[float] = []
                for i, pkt in enumerate(pkts):
                    t0 = time.perf_counter()
                    dec._dec.decode_views(pkt, dec.views, dec.pc, len(pkt))
                    dt.append(time.perf_counter() - t0)
                    for p in range(3):
                        ps[p].append(psnr(ref_frames[i][p], dec.planes[p], maxv))
                        maxerr = max(maxerr, int(np.max(np.abs(
                            ref_frames[i][p].astype(np.int32) -
                            dec.planes[p].astype(np.int32)))))
                    if i % ssim_every == 0:
                        ssims.append(ssim_y(ref_frames[i][0], dec.planes[0],
                                            maxv))
            # PSNR=Infinity 保留非标准字面量（与既有 rd_result 同构，
            # 机器解析方需处理——REGRESSION_BASELINE ANC-18 已注明）
            row = dict(entropy=tag, qp=qp,
                       bytes_per_frame=total / n,
                       mbps=total * 8 * FPS_NOMINAL / n / 1e6,
                       enc_ms=round(enc_ms, 2),
                       dec_p50_ms=round(pct(dt, 0.5) * 1000, 3),
                       dec_p95_ms=round(pct(dt, 0.95) * 1000, 3),
                       psnr_y=round(float(np.mean(ps[0])), 3),
                       psnr_u=round(float(np.mean(ps[1])), 3),
                       psnr_v=round(float(np.mean(ps[2])), 3),
                       ssim_y=round(float(np.mean(ssims)), 5),
                       maxerr=maxerr)
            rows.append(row)
            print(f"  {tag} qp={qp:2d} {row['mbps']:7.2f} Mbps  "
                  f"Y {row['psnr_y']} dB  SSIM {row['ssim_y']:.5f}  "
                  f"dec p95 {row['dec_p95_ms']:.2f} ms  err {maxerr}",
                  flush=True)
    return rows


def plan_dict(args: argparse.Namespace) -> dict:
    """构造 dry-run 计划（纯函数，供单测）。"""
    cases = resolve_cases(args)
    return {
        "cases": {name: {**case, "src_exists": Path(case["src"]).is_file()}
                  for name, case in cases.items()},
        "qps": args.qps,
        "entropies": args.entropies,
        "frames": args.frames,
        "threads": args.threads,
        "ssim_every": args.ssim_every,
        "generations": args.generations,
        "gen_qps": args.gen_qps,
        "out_root": str(args.out_root),
        "out_dir_template": "<out_root>/<git_head>/<dylib8>/<manifest8>/<utc>",
    }


def print_plan(plan: dict) -> None:
    """打印 dry-run 计划（人类可读）。"""
    print("[plan] rd_result 参数化复现（dry-run，不加载 dylib、不测量）")
    for name, case in plan["cases"].items():
        print(f"[plan] case {name}: {case['w']}x{case['h']} "
              f"profile={case['profile']} pf={case['pixel_format']} "
              f"bd={case['bit_depth']}")
        print(f"[plan]   src = {case['src']} exists = {case['src_exists']}")
        if not case["src_exists"]:
            print("[plan]   WARNING: 源不存在，正式运行将失败"
                  "（dry-run 仅告警）")
    print("[plan] qps          =", plan["qps"])
    print("[plan] entropies    =", plan["entropies"])
    print("[plan] frames/threads/ssim_every =",
          plan["frames"], "/", plan["threads"], "/", plan["ssim_every"])
    print("[plan] generations  =", plan["generations"],
          "gen_qps =", plan["gen_qps"])
    print("[plan] out_root     =", plan["out_root"])
    print("[plan] out_dir      =", plan["out_dir_template"],
          "(rd_result.json)")
    print("[plan] 参数与输出路径自检通过")


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="rd_result 参数化复现（QP 档位/熵模式/分辨率案例/"
                    "素材/线程从 CLI 传入；BENCH-0005 / ANC-18）")
    ap.add_argument("--res", nargs="+", default=["2k", "4k"],
                    choices=sorted(CASES.keys()),
                    help="预设分辨率案例（2k=422 10-bit，4k=444 12-bit），"
                         "可多选，默认 2k+4k（与 bench_rd_baseline 一致）；"
                         "与 --input 二选一")
    ap.add_argument("--input",
                    help="自定义源（ProRes/DNxHR 等可解出 planar YUV 的"
                         "文件；相对路径按仓库根解析）。给出时忽略 --res")
    ap.add_argument("--width", type=int, default=1920,
                    help="自定义案例宽度（默认 1920）")
    ap.add_argument("--height", type=int, default=1080,
                    help="自定义案例高度（默认 1080）")
    ap.add_argument("--profile", type=int, default=3,
                    help="TOPOS profile（3=Standard/422，5=Pro444/444；"
                         "默认 3）")
    ap.add_argument("--pixel-format", type=int, default=0, choices=(0, 1, 3),
                    help="0=422、1=444、3=CFA（默认 0）")
    ap.add_argument("--bit-depth", type=int, default=10, choices=(10, 12, 16),
                    help="位深（默认 10）")
    ap.add_argument("--qps", default="20,25,30,35,40,45,50,55",
                    help="QP 档位（逗号分隔；默认 20-55 步进 5）")
    ap.add_argument("--entropy", default="V1,V2",
                    help="熵模式子集（逗号分隔 V1/V2；默认两者）")
    ap.add_argument("--frames", type=int, default=64,
                    help="参考帧数（默认 64，与 6b3dfe9d 锚点一致）")
    ap.add_argument("--threads", type=int, default=16,
                    help="slice threads（默认 16）")
    ap.add_argument("--ssim-every", type=int, default=4,
                    help="每 N 帧取样一次 SSIM（默认 4）")
    ap.add_argument("--generations", type=int, default=0,
                    help="代际曲线代数（0=跳过，默认 0；>0 时启用，"
                         "建议 10）")
    ap.add_argument("--gen-qps", default="30,45",
                    help="代际曲线 QP（逗号分隔；默认 30,45）")
    ap.add_argument("--out-root", default=str(PROJECT_ROOT / "bench_out"),
                    help="输出根目录（默认 bench_out）")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="允许工作树有未提交改动；结果标记 git_dirty")
    ap.add_argument("--dry-run", action="store_true",
                    help="自检参数与输出路径并打印计划后退出（无需媒体/"
                         "dylib，退出码 0）")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_arg_parser()
    args = ap.parse_args(argv)
    args.qps = parse_int_list(args.qps, "qps")
    args.gen_qps = parse_int_list(args.gen_qps, "gen-qps")
    args.entropies = parse_entropies(args.entropy)
    if args.frames <= 0:
        raise SystemExit("frames 必须为正数")
    if args.threads <= 0:
        raise SystemExit("threads 必须为正数")
    if args.ssim_every <= 0:
        raise SystemExit("ssim-every 必须为正数")
    if args.generations < 0:
        raise SystemExit("generations 必须 >= 0")
    if args.input and not Path(args.input).is_absolute():
        args.input = str(PROJECT_ROOT / args.input)

    if args.dry_run:
        print_plan(plan_dict(args))
        return 0

    cases = resolve_cases(args)
    for name, case in cases.items():
        src = Path(case["src"])
        if not src.is_file():
            raise SystemExit(f"案例 {name} 源不存在：{src}")

    try:
        manifest = build_manifest(allow_dirty=args.allow_dirty)
    except ManifestMismatch as exc:
        print(f"MANIFEST MISMATCH: {exc}", file=sys.stderr)
        return 2
    manifest["threads_effective"] = args.threads

    from src.shared.codec.topos_binding import ToposCodec

    codec_lib = ToposCodec()
    codec_lib.set_slice_threads(args.threads)

    manifest_json = json.dumps(manifest, sort_keys=True).encode()
    manifest_sha8 = hashlib.sha256(manifest_json).hexdigest()[:8]
    utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    out_dir = (Path(args.out_root) / manifest["git_head"]
               / manifest["dylib_sha256"][:8] / manifest_sha8 / utc)
    out_dir.mkdir(parents=True, exist_ok=False)

    print("[manifest]", json.dumps({k: manifest[k] for k in (
        "git_head", "git_dirty", "dylib_sha256", "slice_threads")},
        ensure_ascii=False))

    result = {
        "manifest": manifest,
        "repro": {
            "tool": "tools/bench_rd_scan_repro.py",
            "argv": sys.argv[1:],
        },
        "cases": {},
        "generations": {},
    }
    for name, case in cases.items():
        print(f"===== {name} {case['w']}x{case['h']} "
              f"pf={case['pixel_format']} bd={case['bit_depth']} =====",
              flush=True)
        frames = load_frames(Path(case["src"]), case["w"], case["h"],
                             args.frames, case["pixel_format"])
        print(f"  参考 {len(frames)} 帧（无损源平面直通）", flush=True)
        result["cases"][name] = dict(
            case={k: v for k, v in case.items() if k != "src"},
            frames=len(frames),
            rows=sweep_rows(codec_lib, case, frames, args.qps,
                            args.entropies, args.ssim_every))
        if args.generations > 0:
            gen_frames = frames[:max(16, args.frames // 2)]
            result["generations"][name] = generation_curve(
                codec_lib, case, gen_frames, args.gen_qps, args.generations)

    (out_dir / "rd_result.json").write_text(
        json.dumps(result, indent=1, ensure_ascii=False))
    print(f"[result] {out_dir / 'rd_result.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
