#!/usr/bin/env python3
"""Generate the O1 scalar color/alpha reference report.

This report is intentionally a small, deterministic reference-vector report;
it does not claim GPU or external-host equivalence.  Those consumers must
compare against the same vectors before O1 can be promoted from reference
complete to cross-host complete.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import numpy as np

from src.shared.color.types import ColorMatrix, ColorRange, ColorTransfer
from src.shared.media.color_decode import ColorConverter, premultiply_alpha, unpremultiply_alpha

def _hash_script() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _roundtrip(converter: ColorConverter, transfer: ColorTransfer, values: np.ndarray) -> float:
    linear = converter.decode_transfer_to_linear(values, transfer)
    encoded = converter.linear_to_display(linear, transfer)
    return float(np.max(np.abs(encoded - values)))


def build_report() -> dict:
    converter = ColorConverter()
    vectors = []
    # Neutral gray is an invariant for every supported YUV matrix and both
    # range conventions; this catches matrix/range drift without an external
    # color library in the baseline job.
    for matrix in (ColorMatrix.BT601, ColorMatrix.BT601_PAL, ColorMatrix.BT709, ColorMatrix.BT2020_NC):
        for range_name, yuv in (("full", (127.5, 127.5, 127.5)),
                                ("limited", (125.5, 128.0, 128.0))):
            y = np.full((2, 2), yuv[0], dtype=np.float32)
            u = np.full((2, 2), yuv[1], dtype=np.float32)
            v = np.full((2, 2), yuv[2], dtype=np.float32)
            rgb = converter.yuv_to_rgb(y, u, v, matrix,
                                       ColorRange.FULL if range_name == "full" else ColorRange.LIMITED)
            vectors.append({"name": f"neutral-{matrix.value}-{range_name}",
                            "max_abs_error": float(np.max(np.abs(rgb - 0.5)))})
    transfer = {
        name: _roundtrip(converter, value, np.linspace(0.0, 1.0, 257, dtype=np.float32))
        for name, value in (("srgb", ColorTransfer.SRGB), ("bt709", ColorTransfer.BT709),
                            ("pq", ColorTransfer.PQ), ("hlg", ColorTransfer.HLG))
    }
    rgb = np.array([[[0.25, 0.5, 0.75], [0.1, 0.2, 0.3]]], dtype=np.float32)
    alpha = np.array([[0.0, 0.5]], dtype=np.float32)
    premultiplied = premultiply_alpha(rgb, alpha)
    premultiplied[0, 0] = rgb[0, 0]
    alpha_error = float(np.max(np.abs(unpremultiply_alpha(premultiplied, alpha) - rgb)))
    return {
        "schema_version": 1,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "reference_implementation": "src.shared.media.color_decode.ColorConverter",
        "script_sha256": _hash_script(),
        "status": "scalar_reference_vectors",
        "profiles": {"preview": {"pixel_format": "yuv422", "alpha": ["absent", "a16", "bounded"]},
                     "hq": {"pixel_format": "gbr444", "bit_depth": 10},
                     "xq": {"pixel_format": "gbr444", "bit_depth": 12}},
        "neutral_vectors": vectors,
        "transfer_roundtrip_max_abs_error": transfer,
        "alpha_zero_rgb_max_abs_error": alpha_error,
        "deferred": ["GPU parity", "file explorer professional display", "ICC tag semantic comparison", "DeltaE2000 against external reference"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=REPO / "docs/image/reports/color_reference_o1.json")
    args = parser.parse_args()
    report = build_report()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
