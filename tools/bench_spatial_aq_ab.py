#!/usr/bin/env python3
"""空间 AQ 收益 A/B 官方复现脚本（BENCH-0011）。

裁决证据冲突：commit 64e7e219 的 aq_retier 显示 AQ 省 1.5–4% 码率
（ratio 0.9617–0.9858），而 p3_spatial_aq（bench_out/topos_quality/
p3_spatial_aq_ab.json）显示同码率下三项全负（ΔPSNR −0.18~−0.27 dB、
编码 −7.3~−12%、码率 +0.03~+0.2%）。两轮协议不同（retier 矩阵 vs
p3 单次 A/B、无 CI、无交替），冲突未定。本脚本提供**同素材、同码率目标
（tier bpp 码控）、aq off/on 两臂交替多轮**的采集通道，输出
PSNR / 编码 fps / 码率三项与预注册判据，满足 HISTORICAL 规则 2 的
交替 A/B + 中位数 + bootstrap 95% CI 形态。

预注册判据（写入 result.json 的 verdict；--rate-parity/--psnr-gate/
--fps-floor 可调）：

1. 码率配平前提：|bytes_aq / bytes_off − 1| ≤ --rate-parity（默认 2%）。
   不满足 → verdict="rate_mismatch"（其余判据失效）。
2. 画质：ΔPSNR_Y ≥ +0.1 dB → "aq_better"；≤ −0.1 dB → "aq_worse"；
   否则 "no_effect"。
3. 速度预算：fps 比值（aq/off）bootstrap 95% CI 下界 ≥ --fps-floor
   （默认 0.90，即编码吞吐劣化不得超 10%）。

编码确定性核对：同臂多轮的输出逐轮 sha256 一致则记
deterministic=true（速度/码率 CI 才有意义）；不一致时如实记录并降级
为单轮观察。画质度量与 tools/bench_p3_spatial_aq.py 同口径
（母版 4K_prores4444 reformat 参考 + Metric 累计）。

示例：
    TOPOS_CODEC_LIB=... .venv/bin/python tools/bench_spatial_aq_ab.py \
        --material 2k --tiers proxy lt --rounds 3 --allow-dirty

    # 无媒体环境自检（参数/输出路径校验 + 打印计划，不触碰 dylib）：
    .venv/bin/python tools/bench_spatial_aq_ab.py --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
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

import numpy as np  # noqa: E402

from bench_manifest import ManifestMismatch, build_manifest  # noqa: E402
from measure_topos_baseline_quality import (  # noqa: E402
    SIZES,
    Metric,
    iter_reference,
    sample_indices,
)

MEDIA = PROJECT_ROOT / "tests" / "test_videos"
MASTER = MEDIA / "4K_prores4444.mov"
OUT_DIR_DEFAULT = PROJECT_ROOT / "bench_out" / "spatial_aq_ab"
FPS = Fraction(25, 1)
#: tier → 编码源（与 p3 口径一致：同档位同分辨率 ProRes 基准作源）。
TIER_SOURCES = {
    "proxy": "{size}_prores422Proxy.mov",
    "lt": "{size}_prores422LT.mov",
}
FMT = "yuv422p10le"


def _plane_array(plane) -> np.ndarray:
    return np.ascontiguousarray(np.ndarray(
        (plane.height, plane.width), dtype="<u2",
        buffer=memoryview(plane), strides=(plane.line_size, 2)))


class PlanarFrame:
    """ToposVideoEncoder 帧输入桩（与 bench_p3_spatial_aq 同形状）。"""

    def __init__(self, planes, fmt, w, h, idx):
        self.planes, self.pixel_format = planes, fmt
        self.width, self.height, self.frame_index = w, h, idx
        self.is_planar = True


def bootstrap_ratio_ci(paired: list[tuple[float, float]],
                        n_boot: int = 2000, seed: int = 0xA0B011,
                        confidence: float = 0.95) -> dict | None:
    """成对样本的中位数比值 bootstrap 置信区间（百分位法，纯函数供单测）。

    ``paired`` 为 [(off, on), ...] 逐轮成对样本（轮内交替采集，同轮两臂
    共享机器窗口）。对轮索引重采样，每次取两臂各自中位数的比值
    ``median(on) / median(off)``，按百分位取 [2.5, 97.5]。样本不足
    （< 2 对）或分母退化时返回 None。
    """
    if len(paired) < 2:
        return None
    offs = [float(o) for o, _ in paired]
    ons = [float(n) for _, n in paired]
    if any(v <= 0 for v in offs) or any(v <= 0 for v in ons):
        return None
    rng = random.Random(seed)
    ratios: list[float] = []
    for _ in range(n_boot):
        idx = [rng.randrange(len(paired)) for _ in range(len(paired))]
        base = statistics.median(offs[i] for i in idx)
        probe = statistics.median(ons[i] for i in idx)
        if base > 0:
            ratios.append(probe / base)
    if not ratios:
        return None
    alpha = (1.0 - confidence) / 2.0
    ratios.sort()

    def _pct(data: list[float], q: float) -> float:
        pos = (len(data) - 1) * q
        lo = int(pos // 1)
        hi = min(len(data) - 1, lo + 1)
        frac = pos - lo
        return data[lo] * (1 - frac) + data[hi] * frac

    median_off = statistics.median(offs)
    median_on = statistics.median(ons)
    return {
        "ratio_median": (median_on / median_off if median_off > 0 else None),
        "ci_low": _pct(ratios, alpha),
        "ci_high": _pct(ratios, 1.0 - alpha),
        "n_boot": n_boot,
        "confidence": confidence,
        "method": "paired percentile bootstrap over rounds; "
                  "statistic = median(on)/median(off)",
    }


def iter_topos_frames(codec, path: Path, indices: list[int]):
    """解码指定帧号（与 measure_topos_baseline_quality.iter_topos 同实现，
    但不强制 487 帧——本脚本 --frames 可截短）。"""
    from src.shared.codec.topos_binding import ToposDecoder, ToposMovieFile

    movie = ToposMovieFile(codec, str(path))
    decoder = ToposDecoder(codec)
    try:
        n = int(movie.info().sample_count)
        wanted = [i for i in indices if i < n]
        views = None
        for i in wanted:
            buf, size = movie.packet_into(i)
            fi = decoder.prepare(buf, size)
            if views is None:
                planes = []
                for k in range(int(fi.plane_count)):
                    pw, ph = codec.plane_geometry(fi, k)
                    planes.append(np.empty((ph, pw), dtype="<u2"))
                views = decoder.make_views(planes)
            decoder.decode_views(buf, views, int(fi.plane_count), size)
            yield i, tuple(np.array(p) for p in planes)
    finally:
        decoder.close()
        movie.close()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def encode_variant(out_path: Path, src: Path, size: str, tier: str,
                   aq: bool, threads: int, frames: int,
                   width: int, height: int) -> tuple[float, int]:
    """一臂一轮：tier bpp 码控编码 → (encode_fps, video_bytes)。"""
    import av

    from src.shared.codec.color_metadata_config import ColorMetadataConfig
    from src.shared.export.topos_encoder import ToposVideoEncoder

    out_path.unlink(missing_ok=True)
    cfg = SimpleNamespace(
        width=width, height=height, fps=FPS, pix_fmt=FMT, profile=tier,
        crf=None, has_alpha=False, entropy_mode="rans2",
        aq_mode="on" if aq else "off", rdo_mode="off", gop="intra",
        color_metadata=ColorMetadataConfig())
    encoder = ToposVideoEncoder(str(out_path), cfg, slice_threads=threads)
    encoder.open()
    t0 = time.perf_counter()
    n = 0
    try:
        with av.open(str(src)) as container:
            for frame in container.decode(container.streams.video[0]):
                if n >= frames:
                    break
                planes = tuple(_plane_array(p) for p in frame.planes[:3])
                encoder.encode_frame(PlanarFrame(planes, FMT, width, height, n))
                n += 1
    finally:
        encoder.close()
    elapsed = time.perf_counter() - t0
    return n / elapsed, out_path.stat().st_size


def verdict_for(delta_psnr_y: float, rate_gap: float, fps_ci: dict | None,
                rate_parity: float, psnr_gate: float,
                fps_floor: float) -> dict:
    """预注册判据（纯函数，供单测）。"""
    if rate_gap > rate_parity:
        return {"verdict": "rate_mismatch",
                "speed_budget": "unknown_rate_mismatch",
                "reason": f"码率差 {rate_gap * 100:.2f}% 超过配平门 "
                          f"{rate_parity * 100:.1f}%，画质/速度判据失效",
                "psnr_delta_y_db": delta_psnr_y,
                "rate_gap_pct": rate_gap * 100.0,
                "fps_ratio_ci": fps_ci,
                "criteria": {
                    "rate_parity": rate_parity,
                    "psnr_gate_db": psnr_gate,
                    "fps_ratio_ci_floor": fps_floor,
                }}
    if delta_psnr_y >= psnr_gate:
        verdict = "aq_better"
    elif delta_psnr_y <= -psnr_gate:
        verdict = "aq_worse"
    else:
        verdict = "no_effect"
    if fps_ci is None:
        speed_budget = "unknown_no_ci"
    else:
        speed_budget = "pass" if fps_ci["ci_low"] >= fps_floor else "fail"
    return {
        "verdict": verdict,
        "speed_budget": speed_budget,
        "psnr_delta_y_db": delta_psnr_y,
        "rate_gap_pct": rate_gap * 100.0,
        "fps_ratio_ci": fps_ci,
        "criteria": {
            "rate_parity": rate_parity,
            "psnr_gate_db": psnr_gate,
            "fps_ratio_ci_floor": fps_floor,
        },
    }


def validate_args(args: argparse.Namespace) -> None:
    if args.rounds <= 0:
        raise SystemExit("rounds 必须为正数")
    if args.frames <= 0:
        raise SystemExit("frames 必须为正数")
    if args.stride <= 0:
        raise SystemExit("stride 必须为正数")
    if args.threads <= 0:
        raise SystemExit("threads 必须为正数")
    if not 0 < args.rate_parity < 1:
        raise SystemExit("rate-parity 必须在 (0,1)")
    if args.fps_floor <= 0:
        raise SystemExit("fps-floor 必须为正数")
    if args.n_boot <= 0:
        raise SystemExit("n-boot 必须为正数")
    unknown = [t for t in args.tiers if t not in TIER_SOURCES]
    if unknown:
        raise SystemExit(f"未知 tier：{unknown}（可选：{list(TIER_SOURCES)}）")


def plan_dict(args: argparse.Namespace) -> dict:
    """dry-run 计划（纯函数，供单测；args.tiers 保持字符串形态）。"""
    tiers = ([t.strip() for t in args.tiers.split(",") if t.strip()]
             if isinstance(args.tiers, str) else list(args.tiers))
    sources = {}
    for tier in tiers:
        name = TIER_SOURCES[tier].format(size=args.material)
        path = MEDIA / name
        sources[f"{args.material}/{tier}"] = {
            "source": str(path), "exists": path.is_file()}
    return {
        "material": args.material,
        "tiers": tiers,
        "frames": args.frames,
        "rounds": args.rounds,
        "rounds_ok": args.rounds >= 3,
        "stride": args.stride,
        "threads": args.threads,
        "rate_parity": args.rate_parity,
        "psnr_gate_db": args.psnr_gate,
        "fps_floor": args.fps_floor,
        "n_boot": args.n_boot,
        "seed": args.seed,
        "sources": sources,
        "master": str(MASTER),
        "master_exists": MASTER.is_file(),
        "out_root": str(Path(args.out_root) if args.out_root else OUT_DIR_DEFAULT),
        "out_dir_template": "<out_root>/<git_head>/<dylib8>/<manifest8>/<utc>",
    }


def print_plan(plan: dict) -> None:
    print("[plan] 空间 AQ A/B（dry-run，不加载 dylib、不测量）")
    print(f"[plan] material={plan['material']} tiers={plan['tiers']} "
          f"frames={plan['frames']} stride={plan['stride']} "
          f"threads={plan['threads']}")
    print(f"[plan] rounds={plan['rounds']}（交替 off/on，"
          f"{'满足' if plan['rounds_ok'] else '不满足（<3 轮无 CI 价值）'}"
          " HISTORICAL 规则 2 形态）")
    for key, info in plan["sources"].items():
        print(f"[plan] source[{key}] = {info['source']} exists = {info['exists']}")
        if not info["exists"]:
            print(f"[plan] WARNING: {key} 编码源不存在，正式运行将失败"
                  "（dry-run 仅告警）")
    print(f"[plan] master = {plan['master']} exists = {plan['master_exists']}")
    if not plan["master_exists"]:
        print("[plan] WARNING: 参考母版不存在，画质度量将失败（dry-run 仅告警）")
    print(f"[plan] 判据: rate-parity={plan['rate_parity']} "
          f"psnr-gate={plan['psnr_gate_db']}dB "
          f"fps-floor={plan['fps_floor']} n_boot={plan['n_boot']} "
          f"seed={plan['seed']}")
    print(f"[plan] out_root = {plan['out_root']}")
    print(f"[plan] out_dir  = {plan['out_dir_template']} (result.json)")
    print("[plan] 参数与输出路径自检通过")


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="空间 AQ 收益 A/B（同素材同码率目标 off/on 交替多轮 + "
                    "bootstrap CI + 预注册判据；BENCH-0011）")
    ap.add_argument("--material", default="2k", choices=sorted(SIZES),
                    help="素材分辨率档（默认 2k，与 p3 证据同口径）")
    ap.add_argument("--tiers", default="proxy,lt",
                    help=f"逗号分隔 tier 子集（可选 {list(TIER_SOURCES)}）")
    ap.add_argument("--frames", type=int, default=487,
                    help="编码帧数（默认 487 全片，与 p3 口径一致）")
    ap.add_argument("--rounds", type=int, default=3,
                    help="off/on 交替轮数（默认 3；<3 时无 CI 价值并写 "
                         "rounds_warning）")
    ap.add_argument("--stride", type=int, default=20,
                    help="画质度量抽样帧步长（默认 20，与 p3 一致）")
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--rate-parity", type=float, default=0.02,
                    help="码率配平门（默认 0.02=2%%）")
    ap.add_argument("--psnr-gate", type=float, default=0.1,
                    help="ΔPSNR_Y 判据门（dB，默认 0.1）")
    ap.add_argument("--fps-floor", type=float, default=0.90,
                    help="fps 比值 CI 下限速度预算（默认 0.90）")
    ap.add_argument("--n-boot", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=0xA0B011)
    ap.add_argument("--out-root", default="",
                    help="输出根目录（默认 bench_out/spatial_aq_ab）")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="允许工作树有未提交改动；结果标记 git_dirty")
    ap.add_argument("--dry-run", action="store_true",
                    help="自检参数与输出路径并打印计划后退出（无需媒体/"
                         "dylib，退出码 0）")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    tiers = ([t.strip() for t in args.tiers.split(",") if t.strip()]
             if isinstance(args.tiers, str) else list(args.tiers))
    args.tiers = tiers
    validate_args(args)
    if args.dry_run:
        print_plan(plan_dict(args))
        return 0

    try:
        manifest = build_manifest(allow_dirty=args.allow_dirty)
    except ManifestMismatch as exc:
        print(f"MANIFEST MISMATCH: {exc}", file=sys.stderr)
        return 2

    from src.shared.codec.topos_binding import ToposCodec

    out_root = Path(args.out_root) if args.out_root else OUT_DIR_DEFAULT
    work_dir = out_root / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    width, height = SIZES[args.material]

    rounds_warning = None
    if args.rounds < 3:
        rounds_warning = (f"rounds={args.rounds} < 3：无 CI 价值，结果只能作"
                          "单次观察（HISTORICAL 规则 2 / BENCH-0011）")
        print(f"[WARNING] {rounds_warning}")

    codec = ToposCodec()
    sampled = sample_indices(args.stride)
    sampled = [i for i in sampled if i < args.frames]
    sample_set = set(sampled)
    results: dict[str, dict] = {}

    for tier in args.tiers:
        src = MEDIA / TIER_SOURCES[tier].format(size=args.material)
        if not src.is_file():
            raise SystemExit(f"编码源不存在：{src}")
        rounds: dict[bool, list[dict]] = {False: [], True: []}
        outputs: dict[bool, Path] = {}
        # 逐轮交替 off/on（偶数轮 off 先、奇数轮 on 先），同轮两臂共享窗口
        for round_idx in range(args.rounds):
            order = (False, True) if round_idx % 2 == 0 else (True, False)
            for aq in order:
                out_path = (work_dir /
                            f"aq_{args.material}_{tier}_"
                            f"{'on' if aq else 'off'}_r{round_idx}.mov")
                fps, nbytes = encode_variant(
                    out_path, src, args.material, tier, aq, args.threads,
                    args.frames, width, height)
                rounds[aq].append({
                    "round": round_idx, "encode_fps": fps,
                    "video_bytes": nbytes, "sha256": sha256_file(out_path),
                    "path": str(out_path),
                })
                outputs[aq] = out_path
                print(f"  [{args.material}/{tier}/{'aq' if aq else 'off'}] "
                      f"r{round_idx}: {fps:.1f} fps, {nbytes / 1e6:.2f} MB",
                      flush=True)

        deterministic = (
            len({r["sha256"] for r in rounds[False]}) == 1
            and len({r["sha256"] for r in rounds[True]}) == 1)

        # 画质：参考一次，两臂锁步（最后一轮产物）
        metrics = {aq: Metric(FMT) for aq in (False, True)}
        streams = {aq: iter_topos_frames(codec, outputs[aq], sampled)
                   for aq in (False, True)}
        for i, ref in iter_reference(args.material, FMT):
            if i not in sample_set:
                continue
            for aq in (False, True):
                _, tp = next(streams[aq])
                metrics[aq].add(ref, tp)
        off_q, on_q = metrics[False].result(), metrics[True].result()

        fps_paired = [(r0["encode_fps"], r1["encode_fps"])
                      for r0, r1 in zip(rounds[False], rounds[True])]
        fps_ci = bootstrap_ratio_ci(fps_paired, n_boot=args.n_boot,
                                    seed=args.seed)
        bytes_off = statistics.median(r["video_bytes"] for r in rounds[False])
        bytes_on = statistics.median(r["video_bytes"] for r in rounds[True])
        rate_gap = abs(bytes_on / bytes_off - 1.0) if bytes_off else 1.0
        delta_psnr_y = on_q["psnr_y"] - off_q["psnr_y"]
        verdict = verdict_for(delta_psnr_y, rate_gap, fps_ci,
                              args.rate_parity, args.psnr_gate, args.fps_floor)

        entry = {
            "encode_rounds": {"off": rounds[False], "on": rounds[True]},
            "deterministic": deterministic,
            "encode_fps_median": {
                "off": statistics.median(r["encode_fps"] for r in rounds[False]),
                "on": statistics.median(r["encode_fps"] for r in rounds[True]),
            },
            "video_bytes_median": {"off": bytes_off, "on": bytes_on},
            "rate_gap_pct": rate_gap * 100.0,
            "quality": {"off": off_q, "on": on_q},
            "delta": {
                "psnr_y_db": delta_psnr_y,
                "psnr_u_db": on_q["psnr_u"] - off_q["psnr_u"],
                "psnr_v_db": on_q["psnr_v"] - off_q["psnr_v"],
                "ssim_y": on_q["ssim_y"] - off_q["ssim_y"],
                "mae_y_norm": (on_q["mae_y_norm"] - off_q["mae_y_norm"]),
            },
            "verdict": verdict,
        }
        results[f"{args.material}/{tier}"] = entry
        ci_str = (f"[{fps_ci['ci_low']:.3f},{fps_ci['ci_high']:.3f}]"
                  if fps_ci else "无（<2 轮）")
        print(f"[verdict] {args.material}/{tier}: "
              f"ΔPSNR_Y={delta_psnr_y:+.3f} dB "
              f"码率差={rate_gap * 100:+.2f}% fps比CI={ci_str} "
              f"=> {verdict['verdict']}/{verdict['speed_budget']}",
              flush=True)

    result = {
        "manifest": manifest,
        "repro": {
            "tool": "tools/bench_spatial_aq_ab.py",
            "argv": sys.argv[1:],
        },
        "config": {
            "material": args.material,
            "tiers": list(args.tiers),
            "frames": args.frames,
            "rounds": args.rounds,
            "stride": args.stride,
            "threads": args.threads,
            "rate_parity": args.rate_parity,
            "psnr_gate_db": args.psnr_gate,
            "fps_floor": args.fps_floor,
            "n_boot": args.n_boot,
            "seed": args.seed,
        },
        "rounds_warning": rounds_warning,
        "scope": {
            "boundary": "同素材同码率目标（tier bpp 码控）aq off/on 交替 A/B",
            "conflict": "64e7e219 aq_retier（省 1.5–4% 码率） vs p3_spatial_aq"
                        "（三项全负）—— BENCH-0011 复现通道",
        },
        "results": results,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest_json = json.dumps(manifest, sort_keys=True).encode()
    manifest_sha8 = hashlib.sha256(manifest_json).hexdigest()[:8]
    utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    out_dir = (out_root / manifest["git_head"] / manifest["dylib_sha256"][:8]
               / manifest_sha8 / utc)
    out_dir.mkdir(parents=True, exist_ok=False)
    out_file = out_dir / "result.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print("[manifest]", json.dumps({k: manifest[k] for k in (
        "git_head", "git_dirty", "dylib_sha256", "slice_threads")},
        ensure_ascii=False))
    print(f"[result] {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
