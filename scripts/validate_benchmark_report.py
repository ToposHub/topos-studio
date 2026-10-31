#!/usr/bin/env python3
"""Validate a Topos image/video benchmark JSON report.

This is intentionally dependency-free.  CI may additionally validate the
JSON against the published schemas with ``jsonschema``; this command provides
the stable baseline check on developer machines and clean SDK environments.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _required(obj: dict[str, Any], keys: tuple[str, ...], where: str) -> None:
    missing = [key for key in keys if key not in obj]
    if missing:
        raise ValueError(f"{where}: missing {', '.join(missing)}")


def _validate_reduced_decode(report: dict[str, Any]) -> str:
    _required(report, ("schema_version", "benchmark", "generated_at_utc",
                       "environment", "source", "request", "threads",
                       "cache_policy", "modes"), "reduced decode report")
    if report["schema_version"] != 1:
        raise ValueError(f"reduced decode report: unsupported schema_version {report['schema_version']!r}")
    if report["benchmark"] != "topos_reduced_decode":
        raise ValueError("reduced decode report: benchmark must be topos_reduced_decode")
    _required(report["source"], ("path", "width", "height", "plane_count",
                                  "pixel_format", "bit_depth", "packet_sha256",
                                  "packet_bytes"), "reduced decode source")
    _required(report["request"], ("scale_divisor", "scale", "target_width",
                                   "target_height"), "reduced decode request")
    if report["request"]["scale_divisor"] not in (2, 3, 4, 8):
        raise ValueError("reduced decode request: scale_divisor must be 2, 3, 4, or 8")
    _required(report["threads"], ("requested", "effective"), "reduced decode threads")
    if not isinstance(report["modes"], list) or not report["modes"]:
        raise ValueError("reduced decode report: modes must be a non-empty list")
    for index, mode in enumerate(report["modes"]):
        _required(mode, ("name", "cold", "hot"), f"modes[{index}]")
        if mode["name"] not in {"full", "post_scale", "legacy_reduced", "v7_band", "v7_base"}:
            raise ValueError(f"modes[{index}]: unknown mode {mode['name']!r}")
        for state in ("cold", "hot"):
            run = mode[state]
            _required(run, ("status",), f"modes[{index}].{state}")
            if run["status"] == "ok":
                _required(run, ("cache_state", "iterations", "latency_ms",
                                "output_bytes", "telemetry"),
                          f"modes[{index}].{state}")
                _required(run["latency_ms"], ("p50", "p95", "p99", "max", "mean"),
                          f"modes[{index}].{state}.latency_ms")
                _required(run["telemetry"], (
                    "packet_bytes_read", "segments_parsed", "segments_skipped",
                    "entropy_symbols", "coefficients_skipped", "idct_samples",
                    "upload_bytes", "frames", "wall_ns", "p50_ns", "p95_ns", "p99_ns"),
                          f"modes[{index}].{state}.telemetry")
            elif run["status"] != "unsupported":
                raise ValueError(f"modes[{index}].{state}: status must be ok or unsupported")
    return "reduced_decode"


def validate(report: dict[str, Any]) -> str:
    if report.get("benchmark") == "topos_reduced_decode":
        return _validate_reduced_decode(report)
    _required(report, ("schema_version", "environment", "source", "profile", "threads"), "report")
    if report["schema_version"] != 1:
        raise ValueError(f"report: unsupported schema_version {report['schema_version']!r}")
    if "stages" in report:
        _required(report, ("cache_state", "stages", "quality"), "image report")
        if report["cache_state"] not in {"cold", "hot", "unknown"}:
            raise ValueError("image report: cache_state must be cold, hot, or unknown")
        if len(report["stages"]) < 7:
            raise ValueError("image report: expected probe, validate, decode, convert, resize and WebP stages")
        for index, stage in enumerate(report["stages"]):
            _required(stage, ("name", "latency_ms", "throughput_fps", "memory_peak_mb"), f"stages[{index}]")
            _required(stage["latency_ms"], ("p50", "p95", "p99", "max", "mean"),
                      f"stages[{index}].latency_ms")
        return "image"
    _required(report, ("metrics",), "video report")
    _required(report["metrics"], ("file_bytes", "bits_per_frame", "quality", "encode", "decode",
                                   "first_frame_ms", "seek", "memory_peak_mb", "cpu_percent"),
              "video report metrics")
    return "video"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    kind = validate(report)
    print(f"valid {kind} benchmark report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
