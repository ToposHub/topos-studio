#!/usr/bin/env python3
"""Q01/Q04 RD 曲线官方采集脚本（BENCH-0013）。

缺口（quality.md §3.1 / codec.md §1.3·§3）：既有 Q01 证据是
``topos_paired_q01_real_source`` 的 30 帧单素材**码率配平**对照（每个档位
一个点），而决策文档 Q04 要求"冻结语料上扫 **≥5 质量点**，绘 RD 曲线、
**码率匹配和画质匹配两表**"——当前证据不足以判定"同质量更省"
（codec.md §3 判定行：证据不在）。

本脚本在同一冻结语料（与 tools/measure_topos_paired_quality.py 相同的
原始 planar 契约）上扫两臂：

- **TOPOS 臂**：固定 QP 扫描（``--topos-qps``，默认 5 点 20/32/44/56/63，
  覆盖到 QP63 地板）；
- **ProRes 参照臂**：ffmpeg prores_ks 的 ``-bits_per_mb`` 码率控制扫描
  （``--prores-bits-per-mb``，默认 6 点 128~4096；编码器下限 128）。

每点产出 bytes + 逐帧 PSNR（对源），随后自动生成决策文档要求的两表与
预注册判定（纯函数 ``rate_matched_table`` / ``quality_matched_table`` /
``q04_verdict``，供单测）：

1. **码率匹配表**：每个 TOPOS 点配比特率最接近的 ProRes 点
   （±2% 内记 matched），报告同码率 ΔPSNR-Y；
2. **画质匹配表**：每个 ProRes 点在 TOPOS RD 曲线上插值出同 PSNR-Y 的
   TOPOS 比特率，报告 bits 比值（<1 即"同质量更省"方向的点态证据）；
3. **Q04 判定**：两臂各 ≥5 点才可判；画质匹配行 ≥60% 比值 ≤0.98 →
   ``topos_same_quality_cheaper``；≥60% ≥1.02 →
   ``topos_costs_more_at_same_quality``；否则 ``mixed_no_verdict``。

**域限定**（与既有证据一致，引用必须随行）：参照为 ffmpeg prores_ks
（非 Apple VideoToolbox），原生 10-bit 422 域（tier proxy/lt/standard/hq）；
12-bit 444 档的 RD 扫描须走 prores_videotoolbox 配平路径
（measure_topos_paired_quality.py 的 VT 12-bit 流程），本脚本显式拒绝。
源为解码得到的 planar 语料时须注明 source_origin（决策文档：冻结语料）。

冻结语料制备（与 paired 工具同法）：
    ffmpeg -i master.mov -f rawvideo -pix_fmt yuv422p10le corpus.raw

示例：
    TOPOS_CODEC_LIB=... .venv/bin/python tools/bench_q01_rd_curve.py \
        --source-raw corpus.raw --width 1920 --height 1080 --frames 30 \
        --tier hq --out-root bench_out/q01_rd_curve --allow-dirty

    # 无媒体环境自检：
    .venv/bin/python tools/bench_q01_rd_curve.py --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import statistics
import subprocess
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
from measure_topos_paired_quality import (  # noqa: E402
    TIERS,
    _encode_topos,
    _frame_error,
    _prores_frames,
    _raw_frames,
    _sha256,
    _topos_frames,
    _validate_source_domain,
    _video_payload_bytes,
)

DEFAULT_OUT_ROOT = PROJECT_ROOT / "bench_out" / "q01_rd_curve"
DEFAULT_TOPOS_QPS = (20, 32, 44, 56, 63)
# prores_ks 编码器下限：bits_per_mb < 128 直接 EINVAL（"too few
# bits per MB, please set at least 128"，本机 ffmpeg 9.0.2 实测）。
DEFAULT_PRORES_BPMB = (128, 256, 512, 1024, 2048, 4096)


def _encode_prores_bpb(ffmpeg: str, source: Path, dest: Path, width: int,
                       height: int, count: int, fps: int, pix_fmt: str,
                       bits_per_mb: int, allow_sw: bool,
                       ) -> tuple[float, list[str]]:
    """prores_ks 按 bits_per_mb 码控编码（profile auto，量化矩阵 auto）。"""
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
        "-pixel_format", pix_fmt, "-video_size", f"{width}x{height}",
        "-framerate", str(fps), "-i", str(source), "-frames:v", str(count),
        "-c:v", "prores_ks", "-bits_per_mb", str(bits_per_mb),
        "-pix_fmt", pix_fmt,
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-colorspace", "bt709", "-color_range", "tv", "-y", str(dest),
    ]
    if allow_sw:
        command.extend(["-allow_sw", "1"])
    started = time.perf_counter()
    subprocess.run(command, check=True)
    return time.perf_counter() - started, command


def mean_channel(per_frame: list[dict], arm: str, channel: str) -> float:
    """逐帧误差记录 → 指定臂/通道的 PSNR 均值（inf 帧以 120 dB 封顶）。"""
    values = [row[arm][channel]["psnr_db"] for row in per_frame]
    capped = [min(v, 120.0) for v in values]
    return statistics.mean(capped) if capped else float("nan")


def interp_bits_at_psnr(points: list[dict], target_psnr: float):
    """在 RD 点列（按 psnr 升序）上插值目标 PSNR 的比特率（纯函数）。

    points 每项含 ``psnr_y`` 与 ``bits``；目标越界返回 None（不外推）。
    """
    ordered = sorted(points, key=lambda p: p["psnr_y"])
    if not ordered:
        return None
    if target_psnr < ordered[0]["psnr_y"] or \
            target_psnr > ordered[-1]["psnr_y"]:
        return None
    for point in ordered:
        if point["psnr_y"] == target_psnr:
            return float(point["bits"])
    for low, high in zip(ordered, ordered[1:]):
        if low["psnr_y"] <= target_psnr <= high["psnr_y"]:
            span = high["psnr_y"] - low["psnr_y"]
            if span <= 0:
                return float(high["bits"])
            frac = (target_psnr - low["psnr_y"]) / span
            return float(low["bits"] + frac * (high["bits"] - low["bits"]))
    return None


def rate_matched_table(topos_points: list[dict], prores_points: list[dict],
                       tolerance: float = 0.02) -> list[dict]:
    """码率匹配表（纯函数）：每 TOPOS 点配比特率最接近的 ProRes 点。

    ``match``：|topos/prores − 1| ≤ tolerance；否则 ``nearest_below``/
    ``nearest_above``（最近点在 topos 之下/之上）。
    """
    rows: list[dict] = []
    for point in topos_points:
        best = min(prores_points, key=lambda p: abs(
            point["bits"] / p["bits"] - 1.0)) if prores_points else None
        if best is None:
            rows.append({"topos_qp": point.get("qp"), "match": "no_reference"})
            continue
        ratio = point["bits"] / best["bits"]
        match = ("matched" if abs(ratio - 1.0) <= tolerance
                 else ("nearest_below" if ratio < 1.0 else "nearest_above"))
        rows.append({
            "topos_qp": point.get("qp"),
            "topos_bits": point["bits"],
            "topos_psnr_y": point["psnr_y"],
            "prores_bits_per_mb": best.get("bits_per_mb"),
            "prores_bits": best["bits"],
            "prores_psnr_y": best["psnr_y"],
            "bits_ratio": ratio,
            "delta_psnr_y_db": point["psnr_y"] - best["psnr_y"],
            "match": match,
        })
    return rows


def quality_matched_table(topos_points: list[dict],
                          prores_points: list[dict]) -> list[dict]:
    """画质匹配表（纯函数）：每 ProRes 点插值同 PSNR-Y 的 TOPOS 比特率。"""
    rows: list[dict] = []
    for point in prores_points:
        topos_bits = interp_bits_at_psnr(topos_points, point["psnr_y"])
        rows.append({
            "prores_bits_per_mb": point.get("bits_per_mb"),
            "prores_bits": point["bits"],
            "prores_psnr_y": point["psnr_y"],
            "topos_bits_at_same_psnr": topos_bits,
            "bits_ratio_topos_over_prores": (
                topos_bits / point["bits"]
                if topos_bits is not None else None),
            "in_range": topos_bits is not None,
        })
    return rows


def q04_verdict(rate_rows: list[dict], quality_rows: list[dict],
                topos_points: int, prores_points: int,
                min_points: int = 5) -> dict:
    """Q04 预注册判定（纯函数）：同质量是否更省。"""
    if topos_points < min_points or prores_points < min_points:
        return {"verdict": "insufficient_points",
                "note": f"决策文档 Q04 要求两臂各 ≥{min_points} 质量点"}
    in_range = [r for r in quality_rows if r["in_range"]]
    if len(in_range) < min_points:
        return {"verdict": "insufficient_quality_overlap",
                "note": "PSNR 覆盖重叠不足，无法插值出 ≥5 个同质量点"}
    ratios = [r["bits_ratio_topos_over_prores"] for r in in_range]
    cheaper = sum(1 for r in ratios if r <= 0.98)
    costlier = sum(1 for r in ratios if r >= 1.02)
    total = len(ratios)
    if cheaper >= 0.6 * total:
        verdict = "topos_same_quality_cheaper"
    elif costlier >= 0.6 * total:
        verdict = "topos_costs_more_at_same_quality"
    else:
        verdict = "mixed_no_verdict"
    return {"verdict": verdict, "cheaper_points": cheaper,
            "costlier_points": costlier, "total_interpolated": total,
            "note": "点态证据（冻结语料、prores_ks 参照）；发布宣称还须"
                    "扩样与盲测（决策文档 Q04 验收线）"}


def parse_int_list(spec: str, name: str, lo: int, hi: int) -> list[int]:
    """解析逗号分隔整数列表（纯函数供单测）。"""
    values: list[int] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        value = int(token)
        if not lo <= value <= hi:
            raise SystemExit(f"{name} 取值必须在 {lo}..{hi}：{value}")
        values.append(value)
    if not values:
        raise SystemExit(f"{name} 不能为空")
    if len(set(values)) != len(values):
        raise SystemExit(f"{name} 含重复值")
    return values


def plan_dict(args: argparse.Namespace) -> dict:
    """dry-run 计划（纯函数供单测）。"""
    return {
        "source_raw": str(args.source_raw),
        "source_exists": Path(args.source_raw).is_file(),
        "tier": args.tier,
        "width": args.width, "height": args.height,
        "frames": args.frames, "fps": args.fps,
        "topos_qps": list(args.topos_qps),
        "prores_bits_per_mb": list(args.prores_bits_per_mb),
        "points_ok": (len(args.topos_qps) >= 5
                      and len(args.prores_bits_per_mb) >= 5),
        "out_root": str(args.out_root),
        "out_dir_template": "<out_root>/<git_head>/<dylib8>/<manifest8>/<utc>",
    }


def print_plan(plan: dict) -> None:
    print("[plan] Q01/Q04 RD 曲线采集（dry-run，不加载 dylib、不编码）")
    print("[plan] tier           =", plan["tier"],
          "（prores_ks 原生 10-bit 422 域）")
    print("[plan] topos QPs      =", plan["topos_qps"], "（固定 QP 扫描）")
    print("[plan] prores bits/mb =", plan["prores_bits_per_mb"],
          "（prores_ks 码控扫描）")
    print("[plan] points >= 5    =", plan["points_ok"],
          "（决策文档 Q04 硬要求；不满足正式运行直接拒绝）")
    print("[plan] source_raw     =", plan["source_raw"],
          "exists =", plan["source_exists"])
    if not plan["source_exists"]:
        print("[plan] WARNING: 冻结语料不存在，正式运行将失败"
              "（dry-run 仅告警；制备命令见脚本 docstring）")
    print("[plan] 输出           =", plan["out_root"],
          "（result.json + artifacts/ 点级 mov）")
    print("[plan] 参数与输出路径自检通过")


def validate_args(args: argparse.Namespace) -> None:
    if args.width <= 0 or args.height <= 0 or args.frames <= 0 \
            or args.fps <= 0:
        raise SystemExit("宽高、帧数、帧率必须为正数")
    if args.threads <= 0:
        raise SystemExit("threads 必须为正数")
    if args.tier in ("4444", "4444xq"):
        raise SystemExit(
            f"tier={args.tier} 为 12-bit 444 域：prores_ks 不原生支持，"
            "须走 measure_topos_paired_quality.py 的 prores_videotoolbox "
            "配平路径（本脚本显式拒绝，防静默位深转换伪比较）")
    if len(args.topos_qps) < 5 or len(args.prores_bits_per_mb) < 5:
        raise SystemExit("决策文档 Q04 要求两臂各 ≥5 质量点"
                         f"（当前 topos {len(args.topos_qps)} / "
                         f"prores {len(args.prores_bits_per_mb)}）")


def resolve_out_dir(out_root: Path, manifest: dict) -> Path:
    """P0-04 树输出目录：<head>/<dylib8>/<manifest8>/<utc>。"""
    manifest_json = json.dumps(manifest, sort_keys=True).encode()
    manifest_sha8 = hashlib.sha256(manifest_json).hexdigest()[:8]
    utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return (out_root / manifest["git_head"] / manifest["dylib_sha256"][:8]
            / manifest_sha8 / utc)


def measure_point(label: str, source: Path, work: Path, args, qp: int,
                  bpb: int, codec, ffmpeg: str) -> dict:
    """编码并评分一个质量点（qp 与 bpb 恰有一个生效，另一臂占位 None）。"""
    pix_fmt, _profile = TIERS[args.tier]
    bits = 10 if "10le" in pix_fmt else 12
    per_frame: list[dict] = []
    def _score(decoded) -> list[dict]:
        raw = _raw_frames(source, args.width, args.height, pix_fmt,
                          args.frames)
        rows: list[dict] = []
        try:
            for _index in range(args.frames):
                rows.append({"candidate": _frame_error(
                    next(raw), next(decoded), bits)})
        finally:
            decoded.close()
        return rows

    if qp is not None:
        top_path = work / f"topos_qp{qp}.mov"
        _encode_topos(source, top_path, args.width, args.height,
                      args.frames, args.fps, args.tier, pix_fmt,
                      args.threads, qp, None)
        per_frame = _score(_topos_frames(codec, top_path))
        out_path = top_path
        encoder = "ToposVideoEncoder fixed-qp"
    else:
        pro_path = work / f"prores_bpb{bpb}.mov"
        _encode_prores_bpb(ffmpeg, source, pro_path, args.width,
                           args.height, args.frames, args.fps, pix_fmt,
                           bpb, args.allow_sw)
        per_frame = _score(_prores_frames(pro_path, pix_fmt))
        out_path = pro_path
        encoder = f"ffmpeg prores_ks bits_per_mb={bpb}"
    payload = _video_payload_bytes(out_path)
    return {
        "label": label,
        "qp": qp, "bits_per_mb": bpb,
        "path": str(out_path), "sha256": _sha256(out_path),
        "bits": payload * 8,  # 视频载荷比特
        "psnr_y": mean_channel(per_frame, "candidate", "y"),
        "psnr_u": mean_channel(per_frame, "candidate", "u"),
        "psnr_v": mean_channel(per_frame, "candidate", "v"),
        "encoder": encoder,
        "per_frame": per_frame,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Q01/Q04 RD 曲线（≥5 质量点 + 码率/画质匹配两表，"
                    "BENCH-0013）")
    parser.add_argument("--source-raw", required=True, type=Path,
                        help="冻结语料 planar u16 dump（yuv422p10le；"
                             "制备命令见 docstring）")
    parser.add_argument("--source-origin", required=True,
                        help="语料来源标注（如 decoded-lossy-source；"
                             "须随结果走）")
    parser.add_argument("--tier", default="hq", choices=["proxy", "lt",
                                                         "standard", "hq"])
    parser.add_argument("--width", required=True, type=int)
    parser.add_argument("--height", required=True, type=int)
    parser.add_argument("--frames", required=True, type=int)
    parser.add_argument("--fps", type=int, default=25)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--topos-qps",
                        default=",".join(str(v) for v in DEFAULT_TOPOS_QPS),
                        help="TOPOS 固定 QP 扫描点（≥5，0..63）")
    parser.add_argument("--prores-bits-per-mb",
                        default=",".join(str(v) for v in DEFAULT_PRORES_BPMB),
                        help="prores_ks bits_per_mb 扫描点（≥5，"
                             "128..8192；<128 编码器拒绝）")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--allow-sw", action="store_true")
    parser.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT))
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--expect-commit", default=None,
                        help="显式目标 dylib 内嵌 commit（短 SHA；指向非 "
                             "HEAD 构建的库时使用，P0-04 宽松断言）")
    parser.add_argument("--dry-run", action="store_true",
                        help="自检参数并打印计划后退出（无需 dylib/"
                             "ffmpeg/语料）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    args.topos_qps = parse_int_list(args.topos_qps, "--topos-qps", 0, 63)
    args.prores_bits_per_mb = parse_int_list(args.prores_bits_per_mb,
                                             "--prores-bits-per-mb", 128,
                                             8192)
    validate_args(args)

    source = args.source_raw if args.source_raw.is_absolute() \
        else PROJECT_ROOT / args.source_raw
    args.source_raw = source

    if args.dry_run:
        print_plan(plan_dict(args))
        return 0

    from src.shared.codec.topos_binding import ToposCodec

    if not source.is_file():
        raise SystemExit(f"冻结语料不存在：{source}")
    ffmpeg = shutil.which(args.ffmpeg)
    if ffmpeg is None:
        raise SystemExit(f"FFmpeg 不存在: {args.ffmpeg}")
    pix_fmt, _profile = TIERS[args.tier]
    _validate_source_domain(source, args.width, args.height, pix_fmt,
                            args.frames, 10)

    try:
        manifest = build_manifest(allow_dirty=args.allow_dirty,
                                  expect_commit=args.expect_commit)
    except ManifestMismatch as exc:
        print(f"MANIFEST MISMATCH: {exc}", file=sys.stderr)
        return 2

    out_dir = resolve_out_dir(Path(args.out_root), manifest)
    work = out_dir / "artifacts"
    work.mkdir(parents=True, exist_ok=True)
    codec = ToposCodec()

    topos_points: list[dict] = []
    for qp in args.topos_qps:
        point = measure_point(f"topos_qp{qp}", source, work, args,
                              qp=qp, bpb=0, codec=codec, ffmpeg=ffmpeg)
        topos_points.append(point)
        print(f"[rd] topos qp{qp:<3} bits={point['bits']} "
              f"psnr_y={point['psnr_y']:.2f} dB")
    prores_points: list[dict] = []
    for bpb in args.prores_bits_per_mb:
        point = measure_point(f"prores_bpb{bpb}", source, work, args,
                              qp=None, bpb=bpb, codec=codec, ffmpeg=ffmpeg)
        prores_points.append(point)
        print(f"[rd] prores bpb{bpb:<5} bits={point['bits']} "
              f"psnr_y={point['psnr_y']:.2f} dB")

    rate_table = rate_matched_table(topos_points, prores_points)
    quality_table = quality_matched_table(topos_points, prores_points)
    verdict = q04_verdict(rate_table, quality_table,
                          len(topos_points), len(prores_points))

    result = {
        "schema": "bench_q01_rd_curve.v1",
        "manifest": manifest,
        "config": {
            "source": str(source), "source_sha256": _sha256(source),
            "source_origin": args.source_origin,
            "tier": args.tier, "pixel_format": pix_fmt,
            "width": args.width, "height": args.height,
            "frames": args.frames, "fps": args.fps,
            "threads": args.threads,
            "topos_qps": list(args.topos_qps),
            "prores_bits_per_mb": list(args.prores_bits_per_mb),
        },
        "caveats": [
            "参照为 ffmpeg prores_ks（非 Apple VideoToolbox）",
            f"source_origin={args.source_origin}（引用必须随行）",
            "点级 mov 与 sha256 在 artifacts/；正式引用以 result.json 为准",
        ],
        "rd_curve": {"topos": topos_points, "prores": prores_points},
        "rate_matched_table": rate_table,
        "quality_matched_table": quality_table,
        "q04_verdict": verdict,
        "repro": {"argv": sys.argv[1:]},
    }
    out_path = out_dir / "result.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    print("\n[rate-matched]  同码率 ΔPSNR-Y（matched 行为 ±2% 内）：")
    for row in rate_table:
        if "delta_psnr_y_db" in row:
            print(f"  qp{row['topos_qp']:<3} ratio={row['bits_ratio']:.3f} "
                  f"Δ={row['delta_psnr_y_db']:+.2f} dB [{row['match']}]")
    print("[quality-matched] 同 PSNR 的 bits 比（<1 = topos 更省方向）：")
    for row in quality_table:
        ratio = row["bits_ratio_topos_over_prores"]
        print(f"  bpb{row['prores_bits_per_mb']:<5} "
              f"ratio={ratio if ratio is None else round(ratio, 3)} "
              f"in_range={row['in_range']}")
    print(f"[verdict] {verdict['verdict']}")
    shown = out_path
    try:
        shown = out_path.relative_to(PROJECT_ROOT)
    except ValueError:
        pass
    print(f"[result ] {shown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
