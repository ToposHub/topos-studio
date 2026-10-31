#!/usr/bin/env python3
"""Record the M0 machine/input manifest for a macOS GPU export run.

This command does not run an export and therefore cannot claim full GPU. It
freezes the machine, ffmpeg/PyAV and input hashes that the later 60+480x3
acceptance report must reference.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.shared.export.gpu_benchmark_report import (
    MACOS_GPU_REPORT_SCHEMA_VERSION,
    build_input_manifest,
    collect_machine_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    payload = {
        "schema_version": MACOS_GPU_REPORT_SCHEMA_VERSION,
        "benchmark": "macos_full_chain_gpu_manifest",
        "environment": collect_machine_manifest(),
        "inputs": [build_input_manifest(path) for path in args.input],
        "protocol": {
            "warmup_frames": 60,
            "formal_frames": 480,
            "repeats": 3,
            "cache_states": ["cold", "warm"],
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

