#!/usr/bin/env python3
"""Run and report the deterministic V2 versus V7-A size gate.

The native executable compares both syntax forms on the same generated
quantized zigzag coefficients.  This wrapper keeps the gate reproducible,
records the environment, and emits a machine-readable JSON report plus an
optional Markdown summary.
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
    REPO / "native/topos_codec/build/release/topos_v7_size_gate",
    REPO / "native/topos_codec/build/debug/topos_v7_size_gate",
)
INTEGER_FIELDS = (
    "width",
    "height",
    "v2_bytes",
    "v7_bytes",
    "v2_payload_bytes",
    "v7_payload_bytes",
    "directory_bytes",
    "blocks",
    "tokens",
    "eob_bits",
    "active_map_bits",
    "pair_map_bits",
    "selected_map_bits",
    "padding_bits",
    "b0_tokens",
    "b1_tokens",
    "b2_tokens",
    "b3_tokens",
    "b4_tokens",
)


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _git_dirty() -> bool:
    try:
        return bool(subprocess.run(
            ["git", "diff", "--quiet"],
            cwd=REPO,
            check=False,
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
            raise FileNotFoundError(f"size gate executable does not exist: {requested}")
        return requested
    for candidate in DEFAULT_EXECUTABLES:
        if candidate.is_file():
            return candidate
    choices = ", ".join(str(path) for path in DEFAULT_EXECUTABLES)
    raise FileNotFoundError(f"no topos_v7_size_gate executable found; tried: {choices}")


def _run_native(executable: Path) -> list[dict[str, Any]]:
    completed = subprocess.run(
        [str(executable)],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    )
    rows = list(csv.DictReader(completed.stdout.splitlines()))
    if not rows:
        raise ValueError("native size gate produced no CSV rows")
    result: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if row.get("pattern") not in {"flat", "low", "texture", "noise"}:
            raise ValueError(f"row {index}: unknown pattern {row.get('pattern')!r}")
        sample = dict(row)
        for field in INTEGER_FIELDS:
            try:
                sample[field] = int(row[field])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"row {index}: invalid integer field {field}") from exc
        v2_bytes = sample["v2_bytes"]
        v7_bytes = sample["v7_bytes"]
        if v2_bytes <= 0 or v7_bytes <= 0:
            raise ValueError(f"row {index}: packet sizes must be positive")
        sample["delta_pct"] = (v7_bytes - v2_bytes) * 100.0 / v2_bytes
        sample["directory_pct_of_v7"] = (
            sample["directory_bytes"] * 100.0 / v7_bytes
        )
        sample["tokens_by_band"] = {
            f"b{band}": sample[f"b{band}_tokens"] for band in range(5)
        }
        result.append(sample)
    return result


def _pattern_summary(samples: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        grouped[str(sample["pattern"])].append(sample)
    summary: dict[str, dict[str, Any]] = {}
    for pattern, items in sorted(grouped.items()):
        deltas = [float(item["delta_pct"]) for item in items]
        summary[pattern] = {
            "count": len(items),
            "median_delta_pct": statistics.median(deltas),
            "p95_delta_pct": _percentile(deltas, 95),
            "max_delta_pct": max(deltas),
            "min_delta_pct": min(deltas),
        }
    return summary


def build_report(executable: Path) -> dict[str, Any]:
    samples = _run_native(executable)
    deltas = [float(sample["delta_pct"]) for sample in samples]
    median_delta_pct = statistics.median(deltas)
    p95_delta_pct = _percentile(deltas, 95)
    median_pass = median_delta_pct <= 1.0
    p95_pass = p95_delta_pct <= 3.0
    gate_pass = median_pass and p95_pass
    return {
        "schema_version": 1,
        "benchmark": "topos_v7_size_gate",
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
            "description": (
                "Deterministic syntax-only comparison using identical quantized "
                "zigzag coefficients; no pixel DCT or quality claim."
            ),
            "v2": (
                "53-byte frame header + 3 canonical 17-byte slice headers + "
                "canonical VLC slice payload"
            ),
            "v7": (
                "53-byte frame header + 432-byte V7-A directory + five scalar "
                "band payloads per plane"
            ),
            "corpus": sorted({
                f"{sample['width']}x{sample['height']}" for sample in samples
            }),
            "patterns": sorted({str(sample["pattern"]) for sample in samples}),
        },
        "gates": {
            "median_max_pct": 1.0,
            "p95_max_pct": 3.0,
            "median_delta_pct": median_delta_pct,
            "p95_delta_pct": p95_delta_pct,
            "median_pass": median_pass,
            "p95_pass": p95_pass,
            "pass": gate_pass,
        },
        "diagnostics": {
            "min_delta_pct": min(deltas),
            "max_delta_pct": max(deltas),
            "max_directory_pct_of_v7": max(
                float(sample["directory_pct_of_v7"]) for sample in samples
            ),
            "by_pattern": _pattern_summary(samples),
        },
        "samples": samples,
        "decision": (
            "PASS: V7-A scalar syntax stays within the size gate on this "
            "deterministic corpus; keep default-writer enablement behind the "
            "remaining real-media and encode-speed gates."
            if gate_pass
            else
            "FAIL: do not enable V7-A as the default writer; revise syntax or "
            "profile policy before proceeding."
        ),
    }


def render_markdown(report: dict[str, Any]) -> str:
    gates = report["gates"]
    diagnostics = report["diagnostics"]
    status = "PASS" if gates["pass"] else "FAIL"
    lines = [
        "# Topos V7-A size gate",
        "",
        f"Decision: **{status}**",
        "",
        "This is a deterministic syntax-only comparison on identical quantized "
        "zigzag coefficients. It is not a pixel-quality or encode-speed result.",
        "",
        "| Gate | Result | Limit | Status |",
        "| --- | ---: | ---: | --- |",
        f"| Median V7 delta | {gates['median_delta_pct']:.3f}% | "
        f"≤ {gates['median_max_pct']:.3f}% | "
        f"{'PASS' if gates['median_pass'] else 'FAIL'} |",
        f"| P95 V7 delta | {gates['p95_delta_pct']:.3f}% | "
        f"≤ {gates['p95_max_pct']:.3f}% | "
        f"{'PASS' if gates['p95_pass'] else 'FAIL'} |",
        "",
        "## Corpus",
        "",
        f"- Samples: {len(report['samples'])}",
        f"- Resolutions: {', '.join(report['scope']['corpus'])}",
        f"- Patterns: {', '.join(report['scope']['patterns'])}",
        f"- Worst directory share: {diagnostics['max_directory_pct_of_v7']:.3f}%",
        "",
        "## Per-pattern diagnostics",
        "",
        "| Pattern | Count | Median delta | P95 delta | Min | Max |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for pattern, summary in diagnostics["by_pattern"].items():
        lines.append(
            f"| {pattern} | {summary['count']} | "
            f"{summary['median_delta_pct']:.3f}% | "
            f"{summary['p95_delta_pct']:.3f}% | "
            f"{summary['min_delta_pct']:.3f}% | "
            f"{summary['max_delta_pct']:.3f}% |"
        )
    lines.extend([
        "",
        "## Samples",
        "",
        "| Resolution | Pattern | V2 bytes | V7-A bytes | Delta | Directory |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ])
    for sample in report["samples"]:
        lines.append(
            f"| {sample['width']}×{sample['height']} | {sample['pattern']} | "
            f"{sample['v2_bytes']:,} | {sample['v7_bytes']:,} | "
            f"{sample['delta_pct']:.3f}% | "
            f"{sample['directory_pct_of_v7']:.3f}% |"
        )
    lines.extend([
        "",
        report["decision"],
        "",
        f"Generated: {report['generated_at_utc']}",
        f"Commit: {report['environment']['git_commit']}",
        "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--output", type=Path,
                        help="write JSON report here")
    parser.add_argument("--markdown", type=Path,
                        help="write Markdown summary here")
    args = parser.parse_args(argv)
    try:
        executable = _select_executable(args.executable)
        report = build_report(executable)
    except (FileNotFoundError, OSError, subprocess.CalledProcessError,
            ValueError) as exc:
        print(f"v7 size gate failed: {exc}", file=sys.stderr)
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
    return 0 if report["gates"]["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
