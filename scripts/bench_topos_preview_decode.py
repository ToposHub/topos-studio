#!/usr/bin/env python3
"""Measure plan §6.3 codec-only 2K preview decode: XK base-only vs normal 2K.

Runs the native ``topos_scalable_preview_decode`` runner (ADR-C030 minor-4
packets, base-only decode at 1920×1080) against the plain V2 1920×1080 decode
reference, then reports p50/p95 ratios with the §6.3 product targets
(p50 ≤ 1.25×, p95 ≤ 1.50× of normal 2K decode).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import statistics
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DEFAULT_EXECUTABLES = (
    REPO / "native/topos_codec/build/release/topos_scalable_preview_decode",
    REPO / "native/topos_codec/build/debug/topos_scalable_preview_decode",
)
REQUIRED_FIELDS = (
    "source_w", "source_h", "mode", "iteration", "decode_ms",
    "target_w", "target_h", "bytes_read", "bytes_skipped",
    "segments_skipped", "kind",
    "v7b_packet_bytes", "v2_packet_bytes",
)
GATE_P50_MAX_RATIO = 1.25
GATE_P95_MAX_RATIO = 1.50
# 码率换算参考帧率（intra-only 编码：码率 = 每帧字节 × 8 × fps）。
REFERENCE_FPS = 24.0


def _bitrate_mbps(packet_bytes: int, fps: float = REFERENCE_FPS) -> float:
    return packet_bytes * 8.0 * fps / 1_000_000.0


def _bits_per_sample(packet_bytes: int, width: int, height: int,
                     chroma_format: str = "422") -> float:
    """422 10-bit：总采样 = w*h（Y）+ 2*ceil(w/2)*h（U/V）。"""
    chroma_w = (width + 1) // 2 if chroma_format == "422" else width
    samples = width * height + 2 * chroma_w * height
    return packet_bytes * 8.0 / samples


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _percentile(values: list[float], percent: int) -> float:
    ordered = sorted(values)
    return ordered[((len(ordered) - 1) * percent) // 100]


def _select_executable(requested: Path | None) -> Path:
    candidates = (requested,) if requested is not None else DEFAULT_EXECUTABLES
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "topos_scalable_preview_decode not found; build it via "
        "cmake --build native/topos_codec/build/release "
        "-t topos_scalable_preview_decode"
    )


def _run_native(executable: Path, iterations: int, quick: bool,
                kind: str) -> list[dict[str, Any]]:
    command = [str(executable), "--iters", str(iterations), "--kind", kind]
    if quick:
        command.append("--quick")
    completed = subprocess.run(
        command, cwd=REPO, check=True, capture_output=True, text=True,
    )
    rows = list(csv.DictReader(completed.stdout.splitlines()))
    if not rows:
        raise ValueError("preview decode runner produced no CSV rows")
    parsed: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        missing = [field for field in REQUIRED_FIELDS if field not in row]
        if missing:
            raise ValueError(f"row {index}: missing fields {missing}")
        sample: dict[str, Any] = dict(row)
        for field in ("source_w", "source_h", "iteration", "target_w",
                      "target_h", "bytes_read", "bytes_skipped",
                      "segments_skipped", "v7b_packet_bytes",
                      "v2_packet_bytes"):
            sample[field] = int(row[field])
        sample["decode_ms"] = float(row["decode_ms"])
        if sample["kind"] != kind:
            raise ValueError(f"row {index}: kind mismatch {sample['kind']!r}")
        parsed.append(sample)
    return parsed


def _summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    by_mode: dict[tuple, list[float]] = defaultdict(list)
    for sample in samples:
        by_mode[(sample["mode"], sample["source_w"], sample["source_h"])].append(
            sample["decode_ms"])
    ref = by_mode.get(("v2_2k_ref", 1920, 1080))
    if not ref:
        raise ValueError("missing v2_2k_ref reference rows")
    ref_p50 = statistics.median(ref)
    ref_p95 = _percentile(ref, 95)
    resolutions = []
    for (mode, width, height), times in sorted(by_mode.items()):
        if mode != "v7b_base_only":
            continue
        p50 = statistics.median(times)
        p95 = _percentile(times, 95)
        first = next(s for s in samples if s["mode"] == mode
                     and s["source_w"] == width)
        resolutions.append({
            "source": f"{width}×{height}",
            "source_w": width,
            "source_h": height,
            "target": f"{first['target_w']}×{first['target_h']}",
            "p50_ms": p50,
            "p95_ms": p95,
            "fps_p50": 1000.0 / p50,
            "fps_p95": 1000.0 / p95,
            "p50_vs_2k": p50 / ref_p50,
            "p95_vs_2k": p95 / ref_p95,
            "gate_p50": "PASS" if p50 / ref_p50 <= GATE_P50_MAX_RATIO else "FAIL",
            "gate_p95": "PASS" if p95 / ref_p95 <= GATE_P95_MAX_RATIO else "FAIL",
            "base_bytes_read": first["bytes_read"],
            "enhancement_bytes_skipped": first["bytes_skipped"],
            "segments_skipped": first["segments_skipped"],
            "v7b_packet_bytes": first["v7b_packet_bytes"],
            "v7b_mbps_24fps": _bitrate_mbps(first["v7b_packet_bytes"]),
            "v7b_bpp": _bits_per_sample(first["v7b_packet_bytes"], width, height),
            "v2_packet_bytes": first["v2_packet_bytes"],
            "v2_mbps_24fps": _bitrate_mbps(first["v2_packet_bytes"]),
            "v2_bpp": _bits_per_sample(first["v2_packet_bytes"], width, height),
            "iterations": len(times),
        })
    ref_first = next(s for s in samples if s["mode"] == "v2_2k_ref")
    return {
        "reference": {
            "mode": "v2_2k_ref",
            "source": "1920×1080",
            "p50_ms": ref_p50,
            "p95_ms": ref_p95,
            "fps_p50": 1000.0 / ref_p50,
            "fps_p95": 1000.0 / ref_p95,
            "v2_mbps_24fps": _bitrate_mbps(ref_first["v2_packet_bytes"]),
            "v2_bpp": _bits_per_sample(ref_first["v2_packet_bytes"], 1920, 1080),
        },
        "resolutions": resolutions,
        "overall_gate": "PASS" if all(
            r["gate_p50"] == "PASS" and r["gate_p95"] == "PASS"
            for r in resolutions) else "FAIL",
    }


def build_report(executable: Path, iterations: int, quick: bool,
                 kind: str) -> dict[str, Any]:
    samples = _run_native(executable, iterations, quick, kind)
    summary = _summarize(samples)
    return {
        "schema_version": 1,
        "benchmark": "topos_preview_decode_gate",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "os": platform.platform(),
            "machine": platform.machine(),
            "logical_cpu_count": os.cpu_count() or 1,
            "git_commit": _git_commit(),
            "executable": str(executable.resolve()),
        },
        "scope": {
            "content_kind": kind,
            "quick": quick,
            "iterations": iterations,
            "warmup": 3,
            "codec_only": True,
            "writer": "pyramid (ADR-C030 minor-4; 2K sources fall back to "
                      "the as-is base path)",
            "reference_fps_for_bitrate": REFERENCE_FPS,
            "note": (
                "p50/p95 ratios compare tc_v7b_frame_decode(BASE_ONLY) of "
                "each source against plain V2 tc_frame_decode of a "
                "1920×1080 packet on identical synthetic content; §6.3 "
                "targets are product goals (p50 ≤ 1.25×, p95 ≤ 1.50×). "
                "Bitrate = frame packet bytes × 8 × fps at the stated "
                "reference frame rate (intra-only codec, every frame is a "
                "keyframe); bpp is bits per 4:2:2 sample"
            ),
        },
        "gates": {
            "p50_max_ratio": GATE_P50_MAX_RATIO,
            "p95_max_ratio": GATE_P95_MAX_RATIO,
            "status": summary["overall_gate"],
        },
        **summary,
        "samples": samples,
    }


def render_markdown(report: dict[str, Any]) -> str:
    ref = report["reference"]
    fps = report["scope"]["reference_fps_for_bitrate"]
    lines = [
        "# Plan §6.3 codec-only 2K 预览解码与码率表",
        "",
        f"Decision: **{report['gates']['status']}** "
        f"(content: {report['scope']['content_kind']})",
        "",
        f"参照（正常 2K 解码，V2 1920×1080）：p50 {ref['p50_ms']:.3f} ms"
        f"（**{ref['fps_p50']:.0f} fps**）/ p95 {ref['p95_ms']:.3f} ms"
        f"（{ref['fps_p95']:.0f} fps）。",
        "",
        "## 解码 → 2K 帧率（codec-only，base-only 预览路径）",
        "",
        "| 规格（源） | 输出 | p50 ms | **帧率 p50 (fps)** | 帧率 p95 (fps) |"
        " 相对 2K p50 | 门禁 p50 ≤1.25× | 门禁 p95 ≤1.50× |",
        "| --- | --- | ---: | ---: | ---: | ---: | :-: | :-: |",
    ]
    for row in report["resolutions"]:
        lines.append(
            f"| {row['source']} | {row['target']} | "
            f"{row['p50_ms']:.3f} | **{row['fps_p50']:.0f}** | "
            f"{row['fps_p95']:.0f} | "
            f"{row['p50_vs_2k']:.2f}× | {row['gate_p50']} | {row['gate_p95']} |"
        )
    lines.extend([
        "",
        f"## 码率（intra-only：每帧均为关键帧；@ {fps:.0f} fps 换算，"
        "bpp = 每 4:2:2 采样比特）",
        "",
        "| 规格（源） | V7-B 帧 | V7-B Mbps@24fps | V7-B bpp |"
        " V2 帧 | V2 Mbps@24fps | V2 bpp | V7-B/V2 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for row in report["resolutions"]:
        lines.append(
            f"| {row['source']} | {row['v7b_packet_bytes'] / 1e6:.2f} MB | "
            f"{row['v7b_mbps_24fps']:.1f} | {row['v7b_bpp']:.2f} | "
            f"{row['v2_packet_bytes'] / 1e6:.2f} MB | "
            f"{row['v2_mbps_24fps']:.1f} | {row['v2_bpp']:.2f} | "
            f"{row['v7b_packet_bytes'] / row['v2_packet_bytes'] * 100:.1f}% |"
        )
    lines.extend([
        "",
        "## 预览 I/O（base-only 只读 base 段）",
        "",
        "| 规格（源） | base 读入 | 跳过的增强层字节 | 跳过段数 |",
        "| --- | ---: | ---: | ---: |",
    ])
    for row in report["resolutions"]:
        lines.append(
            f"| {row['source']} | {row['base_bytes_read'] / 1024:.0f} KB | "
            f"{row['enhancement_bytes_skipped'] / 1024:.0f} KB | "
            f"{row['segments_skipped']} |"
        )
    lines.extend([
        "",
        f"Generated: {report['generated_at_utc']}",
        f"Commit: {report['environment']['git_commit']}",
        "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--iters", type=int, default=30)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--kind", choices=("flat", "gradient", "grain",
                                           "detail", "mixed"),
                        default="gradient")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args(argv)
    if args.iters < 1:
        parser.error("--iters must be positive")
    try:
        report = build_report(_select_executable(args.executable), args.iters,
                              args.quick, args.kind)
    except (FileNotFoundError, OSError, subprocess.CalledProcessError,
            ValueError) as exc:
        print(f"preview decode gate failed: {exc}", file=sys.stderr)
        return 1
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        sys.stdout.write(payload)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    if args.markdown is not None:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
