#!/usr/bin/env python3
"""codec 档位体积帽 / QP 地板预算门禁（BENCH-0004，2026-09-29）。

数据源：`bench_out/real_codec_bitrate_20260907/result.json`（真实素材
1920×1080/25fps/96 帧，TOPOS 与 ProRes 同组帧对比，码率按最终 MOV 文件
实占、含容器开销）。对照验收线：

1. **98% 体积帽**（决策文档 `docs/codec/TOPOS全格式ProRes对标与终轮
   优化计划_2026-09-27.md` §1 V10-422 验收线："每档同码率非劣和默认档
   容量≤对应 ProRes 的既定 98% 帽须同时验证"；§6/最终判定规则）：
   每档 `topos_mov.bytes ≤ cap_ratio × prores_mov.bytes`；
2. **QP 地板预算**（bench 报告 docs/benchreports/2026-09-28/codec.md
   §1.1：qp 全帧钉死 63 的档位处于帧内 VLC 内核压缩能力上限，实际 bpp
   仍高于档位 target_bpp 即超支）：`qp_min == qp_max == qp_floor` 的档位
   要求 `实际 bpp ≤ 容差 × target_bpp`（实际 bpp 由 bytes 复算，不信任
   result.json 预计算字段）。

任一违规退出码 1（CI 阻断）。本脚本只做判定不改编码器默认参数——
调参（qp 下限放开 / 体积帽重议）属产品决策，见 codec.md §1.1 注记。

用法::

    .venv/bin/python tools/check_codec_budget.py
    .venv/bin/python tools/check_codec_budget.py --json out.json
    .venv/bin/python tools/check_codec_budget.py --exclude pro444  # 跳过
    .venv/bin/python tools/check_codec_budget.py --list  # 仅列表明细
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DEFAULT_RESULT = REPO / "bench_out" / "real_codec_bitrate_20260907" / "result.json"

#: 体积帽：决策文档 §1（V10-422 验收线）。
DEFAULT_CAP_RATIO = 0.98
#: QP 地板：qp 全帧钉死该值 = 码控无余量（real_codec 报告结论）。
DEFAULT_QP_FLOOR = 63
#: 地板档超支容差：1.0 = 任何超支都算违规（严格口径，可用 --放宽）。
DEFAULT_FLOOR_TOLERANCE = 1.0
#: 六档矩阵（result.json 档位 id）。缺失任何一档 = 数据不完整，违规。
EXPECTED_TIERS: tuple[str, ...] = ("proxy", "lt", "standard", "hq", "pro444", "extreme")
#: bpp 复算与 result.json 存档值的允许偏差（数据完整性自检）。
BPP_RECOMPUTE_TOLERANCE = 0.02


def recompute_bpp(bytes_total: int, width: int, height: int, frames: int) -> float:
    """按文件实占字节数复算 bits-per-pixel（不信任存档 bpp 字段）。"""
    if width <= 0 or height <= 0 or frames <= 0:
        raise ValueError(f"invalid geometry: {width}x{height}x{frames}")
    return bytes_total * 8.0 / (width * height * frames)


def check_result(
    result: dict[str, Any],
    *,
    cap_ratio: float = DEFAULT_CAP_RATIO,
    qp_floor: int = DEFAULT_QP_FLOOR,
    floor_tolerance: float = DEFAULT_FLOOR_TOLERANCE,
    exclude: tuple[str, ...] = (),
) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    """逐档判定，返回 (violations, tier_rows, notes)。

    tier_rows 每行：档位、体积比、QP 范围、是否地板钉死、超支倍数、
    判定（PASS/FAIL/SKIP）；violations 为人读违规明细（空 = 通过）。
    """
    violations: list[str] = []
    notes: list[str] = []
    rows: list[dict[str, Any]] = []

    width = int(result.get("width", 0))
    height = int(result.get("height", 0))
    frames = int(result.get("frames", 0))
    if width <= 0 or height <= 0 or frames <= 0:
        violations.append(
            f"schema: result.json 缺少有效 width/height/frames"
            f"（got {width}x{height}x{frames}）")
        return violations, rows, notes

    tiers = result.get("tiers") or []
    by_id: dict[str, dict[str, Any]] = {
        str(t.get("id")): t for t in tiers if isinstance(t, dict)
    }
    for tier_id in EXPECTED_TIERS:
        if tier_id in exclude:
            notes.append(f"skip: {tier_id} 被 --exclude 跳过（不参与判定）")
            continue
        if tier_id not in by_id:
            violations.append(f"tier-missing: 档位 {tier_id} 不在 result.json")
    for tier_id in exclude:
        if tier_id not in by_id:
            notes.append(f"warn: --exclude 的 {tier_id} 本就不在数据中")

    for tier in tiers:
        if not isinstance(tier, dict):
            violations.append("schema: tiers 含非对象条目")
            continue
        tier_id = str(tier.get("id", "?"))
        if tier_id in exclude:
            rows.append({"tier": tier_id, "status": "SKIP", "size_ratio": None,
                         "qp": (tier.get("topos_qp_min"), tier.get("topos_qp_max")),
                         "floor_pinned": None, "overspend": None})
            continue
        try:
            topos_bytes = int(tier["topos_mov"]["bytes"])
            prores_bytes = int(tier["prores_mov"]["bytes"])
            target_bpp = float(tier["topos_target_bpp"])
            qp_min = int(tier["topos_qp_min"])
            qp_max = int(tier["topos_qp_max"])
            stored_bpp = float(tier["topos_mov"]["bpp"])
        except (KeyError, TypeError, ValueError) as exc:
            violations.append(f"schema: {tier_id} 字段缺失/类型错误: {exc}")
            continue

        # —— 数据完整性：bpp 复算 vs 存档 ——
        tier_violations: list[str] = []
        actual_bpp = recompute_bpp(topos_bytes, width, height, frames)
        if abs(actual_bpp - stored_bpp) > BPP_RECOMPUTE_TOLERANCE * max(
                actual_bpp, stored_bpp):
            tier_violations.append(
                f"bpp-arithmetic: {tier_id} 复算 bpp {actual_bpp:.6f} 与存档 "
                f"{stored_bpp:.6f} 偏差超 {BPP_RECOMPUTE_TOLERANCE:.0%}")

        # —— 判定 1：98% 体积帽 ——
        size_ratio = topos_bytes / prores_bytes
        if size_ratio > cap_ratio:
            tier_violations.append(
                f"cap: {tier_id} 体积比 {size_ratio:.2f}× > 帽 "
                f"{cap_ratio:.0%}（topos {topos_bytes} B vs prores "
                f"{prores_bytes} B）——98% 体积帽验收线不满足"
                f"（决策文档 §1 V10-422/§6）")

        # —— 判定 2：QP 地板预算 ——
        floor_pinned = qp_min >= qp_floor and qp_max >= qp_floor and qp_min == qp_max
        overspend: float | None = None
        if floor_pinned:
            overspend = actual_bpp / target_bpp
            if overspend > floor_tolerance:
                tier_violations.append(
                    f"qp-floor: {tier_id} qp 全帧钉死 {qp_floor}（码控无余量）"
                    f"，实际 bpp {actual_bpp:.4f} / 目标 {target_bpp:.5f} "
                    f"= {overspend:.2f}× > 容差 {floor_tolerance:.2f}×"
                    f"——地板档预算不可达（codec.md §1.1）")
        elif qp_max >= qp_floor:
            notes.append(
                f"{tier_id}: qp_max={qp_max} 触及地板但 qp_min={qp_min} 未钉死"
                f"（码控仍可下调），不构成地板违规")

        violations.extend(tier_violations)
        rows.append({
            "tier": tier_id,
            "status": "FAIL" if tier_violations else "PASS",
            "size_ratio": round(size_ratio, 4),
            "qp": (qp_min, qp_max),
            "floor_pinned": floor_pinned,
            "overspend": round(overspend, 4) if overspend is not None else None,
            "cap_ok": size_ratio <= cap_ratio,
        })

    # 已知口径警示（非违规）：Pro444 行 ProRes 输出 12-bit vs TOPOS 10-bit。
    if "pro444" in by_id and "pro444" not in exclude:
        probe = by_id["pro444"].get("prores_probe") or {}
        pix_fmt = str(probe.get("pix_fmt", ""))
        if "12le" in pix_fmt:
            notes.append(
                "pro444: ProRes 输出为 12-bit（yuv444p12le）vs TOPOS 10-bit，"
                "位深不对齐——该档体积比仅作参考，严格同质量比较见 "
                "codec.md §1.1 排除项")
    return violations, rows, notes


def format_report(rows: list[dict[str, Any]], notes: list[str],
                  violations: list[str]) -> str:
    """渲染人读报表（表格 + 注记 + 违规清单）。"""
    lines: list[str] = []
    lines.append(f"{'档位':<10} {'体积比':>8} {'QP范围':>8} {'地板钉死':>6} "
                 f"{'超支':>7} {'判定':>6}")
    lines.append("-" * 56)
    for r in rows:
        qp = r.get("qp")
        qp_txt = f"{qp[0]}-{qp[1]}" if isinstance(qp, tuple) else "—"
        ratio = r.get("size_ratio")
        ratio_txt = f"{ratio:.2f}×" if isinstance(ratio, float) else "—"
        pinned = r.get("floor_pinned")
        pinned_txt = "是" if pinned else ("—" if pinned is None else "否")
        overspend = r.get("overspend")
        overspend_txt = f"{overspend:.2f}×" if isinstance(overspend, float) else "—"
        lines.append(
            f"{str(r.get('tier')):<10} {ratio_txt:>8} {qp_txt:>8} "
            f"{pinned_txt:>6} {overspend_txt:>7} {str(r.get('status')):>6}")
    if notes:
        lines.append("")
        lines.extend(f"注: {n}" for n in notes)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="codec 档位体积帽 / QP 地板预算门禁（BENCH-0004）")
    parser.add_argument("--result", type=Path, default=DEFAULT_RESULT,
                        help=f"result.json 路径（默认 {DEFAULT_RESULT}）")
    parser.add_argument("--cap-ratio", type=float, default=DEFAULT_CAP_RATIO,
                        help=f"体积帽（默认 {DEFAULT_CAP_RATIO}，决策文档 98%% 帽）")
    parser.add_argument("--qp-floor", type=int, default=DEFAULT_QP_FLOOR,
                        help=f"QP 地板值（默认 {DEFAULT_QP_FLOOR}）")
    parser.add_argument("--floor-overspend-tolerance", type=float,
                        default=DEFAULT_FLOOR_TOLERANCE,
                        help="地板档 bpp 超支容差倍数（默认 %.2f，严格）"
                             % DEFAULT_FLOOR_TOLERANCE)
    parser.add_argument("--exclude", action="append", default=[],
                        help="跳过指定档位（可多次；如 --exclude pro444）")
    parser.add_argument("--list", action="store_true",
                        help="仅列表明细，不判 exit code")
    parser.add_argument("--json", type=Path, default=None,
                        help="机器读结果输出路径")
    args = parser.parse_args()

    if not args.result.is_file():
        print(f"✗ result-missing: {args.result}")
        return 1
    try:
        result = json.loads(args.result.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"✗ result-unreadable: {exc}")
        return 1

    violations, rows, notes = check_result(
        result,
        cap_ratio=args.cap_ratio,
        qp_floor=args.qp_floor,
        floor_tolerance=args.floor_overspend_tolerance,
        exclude=tuple(args.exclude),
    )

    print(format_report(rows, notes, violations))
    print()
    for v in violations:
        print(f"✗ {v}")
    verdict = "满足" if not violations else "不满足"
    print(
        f"\n验收线判定：{verdict}（98% 体积帽 × QP 地板预算，"
        f"{len(violations)} 项违规）——证据链："
        f"bench_out/real_codec_bitrate_20260907/result.json ↔ "
        f"docs/benchreports/2026-09-28/codec.md §1.1/§3 ↔ 决策文档"
        f" TOPOS全格式ProRes对标与终轮优化计划_2026-09-27.md §1/§6")
    if args.json:
        args.json.write_text(json.dumps(
            {"ok": not violations, "violations": violations, "tiers": rows,
             "notes": notes,
             "params": {"cap_ratio": args.cap_ratio, "qp_floor": args.qp_floor,
                        "floor_tolerance": args.floor_overspend_tolerance,
                        "exclude": list(args.exclude)}},
            ensure_ascii=False, indent=2) + "\n")
    if args.list:
        return 0
    return 0 if not violations else 1


if __name__ == "__main__":
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    raise SystemExit(main())
