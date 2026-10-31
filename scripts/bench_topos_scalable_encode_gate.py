#!/usr/bin/env python3
"""Measure the RD4-08 V2 versus V7-B scalable encode gate."""
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
    REPO / "native/topos_codec/build/release/topos_scalable_encode_gate",
    REPO / "native/topos_codec/build/debug/topos_scalable_encode_gate",
)
REQUIRED_FIELDS = (
    "width", "height", "iteration", "v2_bytes", "v7b_bytes",
    "v2_encode_ms", "v7b_encode_ms", "v2_psnr_y", "v7b_psnr_y",
    "base_width", "base_height",
    "target_width", "target_height", "ratio_num", "ratio_den", "table",
    "writer", "kind",
)
# Plan §1.1 product main table: 16:9 sources with exact 1, 1/2, 1/3, 1/4, 1/6
# ratios to a 1920×1080 preview.  Anything else (6144×3456, 12288×6480, ...)
# is interoperability appendix material and must be reported separately.
MAIN_TABLE_SOURCES = (
    (1920, 1080), (3840, 2160), (5760, 3240), (7680, 4320), (11520, 6480),
)


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _git_dirty() -> bool:
    try:
        return bool(subprocess.run(
            ["git", "diff", "--quiet"], cwd=REPO, check=False,
        ).returncode)
    except OSError:
        return False


def _percentile(values: list[float], percent: int) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[((len(ordered) - 1) * percent) // 100]


def _select_executable(requested: Path | None) -> Path:
    if requested is not None:
        if not requested.is_file():
            raise FileNotFoundError(f"scalable gate executable missing: {requested}")
        return requested
    for candidate in DEFAULT_EXECUTABLES:
        if candidate.is_file():
            return candidate
    choices = ", ".join(str(path) for path in DEFAULT_EXECUTABLES)
    raise FileNotFoundError(f"no scalable gate executable found; tried: {choices}")


def _run_native(executable: Path, iterations: int, quick: bool,
                table: str = "main", writer: str = "rice",
                kind: str = "gradient") -> list[dict[str, Any]]:
    command = [str(executable), "--iters", str(iterations), "--writer", writer,
               "--kind", kind]
    if quick:
        command.append("--quick")
    if table == "interop":
        command.append("--interop")
    elif table != "main":
        raise ValueError(f"unknown table {table!r}; expected 'main' or 'interop'")
    completed = subprocess.run(
        command, cwd=REPO, check=True, capture_output=True, text=True,
    )
    rows = list(csv.DictReader(completed.stdout.splitlines()))
    if not rows:
        raise ValueError("scalable gate produced no CSV rows")
    parsed: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        missing = [field for field in REQUIRED_FIELDS if field not in row]
        if missing:
            raise ValueError(f"row {index}: missing fields {missing}")
        sample: dict[str, Any] = dict(row)
        for field in ("width", "height", "iteration", "v2_bytes", "v7b_bytes",
                      "base_width", "base_height",
                      "target_width", "target_height", "ratio_num", "ratio_den"):
            try:
                sample[field] = int(row[field])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"row {index}: invalid integer {field}") from exc
        if sample["table"] != table:
            raise ValueError(
                f"row {index}: runner reported table {sample['table']!r}, "
                f"expected {table!r}"
            )
        if sample["writer"] != writer:
            raise ValueError(
                f"row {index}: runner reported writer {sample['writer']!r}, "
                f"expected {writer!r}"
            )
        if sample["kind"] != kind:
            raise ValueError(
                f"row {index}: runner reported kind {sample['kind']!r}, "
                f"expected {kind!r}"
            )
        for field in ("v2_encode_ms", "v7b_encode_ms", "v2_psnr_y", "v7b_psnr_y"):
            try:
                sample[field] = float(row[field])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"row {index}: invalid timing {field}") from exc
        if sample["v2_bytes"] <= 0 or sample["v7b_bytes"] <= 0:
            raise ValueError(f"row {index}: packet sizes must be positive")
        if sample["v2_encode_ms"] <= 0.0 or sample["v7b_encode_ms"] <= 0.0:
            raise ValueError(f"row {index}: timings must be positive")
        sample["size_delta_pct"] = (
            (sample["v7b_bytes"] - sample["v2_bytes"]) * 100.0 /
            sample["v2_bytes"]
        )
        sample["encode_delta_pct"] = (
            (sample["v7b_encode_ms"] - sample["v2_encode_ms"]) * 100.0 /
            sample["v2_encode_ms"]
        )
        parsed.append(sample)
    return parsed


def _summarize(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        grouped[(sample["width"], sample["height"])].append(sample)
    summaries: list[dict[str, Any]] = []
    for (width, height), items in sorted(grouped.items()):
        sizes = [float(item["size_delta_pct"]) for item in items]
        encodes = [float(item["encode_delta_pct"]) for item in items]
        first = items[0]
        summaries.append({
            "width": width,
            "height": height,
            "base_width": first["base_width"],
            "base_height": first["base_height"],
            "target_width": first["target_width"],
            "target_height": first["target_height"],
            "ratio": f"{first['ratio_num']}/{first['ratio_den']}",
            "table": first["table"],
            "iterations": len(items),
            "v2_bytes": items[0]["v2_bytes"],
            "v7b_bytes": items[0]["v7b_bytes"],
            "size_delta_pct": statistics.median(sizes),
            "v2_encode_p50_ms": statistics.median(
                float(item["v2_encode_ms"]) for item in items
            ),
            "v2_encode_p95_ms": _percentile(
                [float(item["v2_encode_ms"]) for item in items], 95
            ),
            "v7b_encode_p50_ms": statistics.median(
                float(item["v7b_encode_ms"]) for item in items
            ),
            "v7b_encode_p95_ms": _percentile(
                [float(item["v7b_encode_ms"]) for item in items], 95
            ),
            "encode_delta_p50_pct": statistics.median(encodes),
            "encode_delta_p95_pct": _percentile(encodes, 95),
            "v2_psnr_y": statistics.median(float(item["v2_psnr_y"]) for item in items),
            "v7b_psnr_y": statistics.median(float(item["v7b_psnr_y"]) for item in items),
        })
    return summaries


def build_report(executable: Path, iterations: int, quick: bool,
                 table: str = "main", writer: str = "rice",
                 kind: str = "gradient") -> dict[str, Any]:
    samples = _run_native(executable, iterations, quick, table, writer, kind)
    summaries = _summarize(samples)
    return {
        "schema_version": 1,
        "benchmark": "topos_scalable_encode_gate",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "os": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python": platform.python_version(),
            "logical_cpu_count": os.cpu_count() or 1,
            "git_commit": _git_commit(),
            "git_dirty": _git_dirty(),
            "executable": str(executable.resolve()),
        },
        "scope": {
            "table": table,
            "writer": writer,
            "content_kind": kind,
            "main_table_sources": [
                f"{width}×{height}" for width, height in MAIN_TABLE_SOURCES
            ],
            "source": "deterministic image_synth content, YUV 4:2:2, 10-bit",
            "v2": "full source geometry via tc_frame_encode",
            "v7b": (
                "rice: base + per-pixel quantized residual via "
                "tc_frame_encode_scalable(max_dim=1920); "
                "pyramid: ADR-C030 minor-4 shared transform pyramid via "
                "tc_v7b_frame_encode_pyramid(max_dim=1920) — one analysis "
                "pass per plane, no second full-resolution scan (P2-04)"
            ),
            "preview_target": (
                "target_width/target_height record the actual bounded base "
                "geometry; main-table 16:9 sources resolve to exactly "
                "1920×1080, interop sources (e.g. 6144×3456 -> 2048×1152) "
                "are reported with their true geometry and never quoted as "
                "product 2K numbers"
            ),
            "iterations": iterations,
            "quick": quick,
            "quality_comparison": "measured_luma_psnr",
            "quality_reason": (
                "the runner decodes both packets and records luma PSNR against "
                "the deterministic source; the multi-content release quality "
                "threshold is still pending"
            ),
        },
        "gates": {
            "total_size_median_max_pct": 1.0,
            "total_size_p95_max_pct": 3.0,
            "encode_p50_max_pct": 5.0,
            "encode_p95_max_pct": 8.0,
            "total_size": {
                "status": "MEASURED",
                "value": statistics.median(
                    float(sample["size_delta_pct"]) for sample in samples
                ),
            },
            "encode_p50": {
                "status": "MEASURED",
                "value": statistics.median(
                    float(sample["encode_delta_pct"]) for sample in samples
                ),
            },
            "encode_p95": {
                "status": "MEASURED",
                "value": _percentile(
                    [float(sample["encode_delta_pct"]) for sample in samples], 95
                ),
            },
            "status": "BLOCKED",
        },
        "diagnostics": {
            "v7b_size_delta_median_pct": statistics.median(
                float(sample["size_delta_pct"]) for sample in samples
            ),
            "v7b_encode_delta_p50_pct": statistics.median(
                float(sample["encode_delta_pct"]) for sample in samples
            ),
            "by_resolution": summaries,
        },
        "samples": samples,
        "decision": (
            "BLOCKED: V7-B writer is measurable, but final joint "
            "quality budgeting and the size/speed limits are not yet passed; "
            "keep default scalable rollout disabled."
        ),
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# RD4-08 scalable encode gate",
        "",
        "Decision: **BLOCKED**",
        "",
        report["decision"],
        "",
        "The native runner compares the V2 full-frame encoder with the complete "
        "V7-B packet on identical deterministic input "
        f"(writer={report['scope']['writer']}, "
        f"content={report['scope']['content_kind']}; "
        "rice = minor-2 base + per-pixel quantized residual, pyramid = "
        "ADR-C030 minor-4 shared pyramid with lossless detail segments). "
        "Both outputs are decoded to measure luma PSNR; multi-content "
        "quality budgeting uses the P0-03 frozen corpus.",
        "",
        "## Final gate",
        "",
        "| Gate | Limit | Status | Reason |",
        "| --- | ---: | --- | --- |",
        "| Total scalable file size median | ≤ +1% | MEASURED | final quality gate pending |",
        "| Total scalable file size p95 | ≤ +3% | MEASURED | final quality gate pending |",
        "| scalable-fast encode p50 | ≤ +5% | MEASURED | final quality gate pending |",
        "| scalable-fast encode p95 | ≤ +8% | MEASURED | final quality gate pending |",
        "",
        "## V7-B measurements",
        "",
        f"Table: **{report['scope']['table']}**, "
        f"writer: **{report['scope']['writer']}**, "
        f"content: **{report['scope']['content_kind']}** "
        f"(ratio = source/preview target)",
        "",
        "| Source | Base | Preview target | Ratio | V7-B bytes / V2 bytes | V2 / V7-B PSNR-Y | V2 p50 ms | V7-B p50 ms | Encode delta |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in report["diagnostics"]["by_resolution"]:
        lines.append(
            f"| {item['width']}×{item['height']} | "
            f"{item['base_width']}×{item['base_height']} | "
            f"{item['target_width']}×{item['target_height']} | "
            f"{item['ratio']} | "
            f"{item['v7b_bytes']:,} / {item['v2_bytes']:,} "
            f"({item['size_delta_pct']:.3f}%) | "
            f"{item['v2_psnr_y']:.2f} / {item['v7b_psnr_y']:.2f} | "
            f"{item['v2_encode_p50_ms']:.3f} | "
            f"{item['v7b_encode_p50_ms']:.3f} | "
            f"{item['encode_delta_p50_pct']:.3f}% |"
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
    parser.add_argument("--iters", type=int, default=3)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--table", choices=("main", "interop"), default="main",
                        help="main = plan §1.1 16:9 product table; "
                             "interop = cinema-ratio appendix")
    parser.add_argument("--writer", choices=("rice", "pyramid"), default="rice",
                        help="rice = public minor-2 writer; "
                             "pyramid = ADR-C030 minor-4 experimental")
    parser.add_argument("--kind", choices=("flat", "gradient", "grain",
                                           "detail", "mixed"),
                        default="gradient",
                        help="image_synth content class (P0-03 corpus 类目)")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args(argv)
    if args.iters < 1:
        parser.error("--iters must be positive")
    try:
        report = build_report(_select_executable(args.executable), args.iters,
                              args.quick, args.table, args.writer, args.kind)
    except (FileNotFoundError, OSError, subprocess.CalledProcessError,
            ValueError) as exc:
        print(f"scalable encode gate failed: {exc}", file=sys.stderr)
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
    # A blocked final gate is a valid engineering report, not a benchmark crash.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
