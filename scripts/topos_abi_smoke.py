#!/usr/bin/env python3
"""Run a small public Topos Image ABI smoke test.

This deliberately exercises only public binding operations: capability query,
probe, planar decode and plane geometry. It is suitable for release-package
checks on macOS/Linux/Windows and does not depend on a particular build tree.
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.shared.codec.topos_image_binding import ToposImageCodec  # noqa: E402


def run(input_path: Path, library: str | None = None) -> dict:
    codec = ToposImageCodec(library)
    info = codec.probe(str(input_path))
    caps = codec.capabilities()
    planes, decoded_info, extra = codec.decode(str(input_path))
    if (info.visible_width, info.visible_height) != (
        decoded_info.visible_width, decoded_info.visible_height,
    ):
        raise RuntimeError("probe/decode geometry mismatch")
    preview_target = (max(1, info.visible_width // 2),
                      max(1, info.visible_height // 2))
    preview_planes, preview_info, preview_extra = codec.decode_preview(
        str(input_path), preview_target)
    if (preview_info.visible_width, preview_info.visible_height) != preview_target:
        raise RuntimeError("preview geometry mismatch")
    if codec._has_native_preview and not preview_extra["target_size_native"]:
        raise RuntimeError("native preview symbol was not used")
    return {
        "status": "ok",
        "platform": platform.platform(),
        "machine": platform.machine(),
        "library": list(codec.library_identity()),
        "input": {
            "path": str(input_path),
            "width": info.visible_width,
            "height": info.visible_height,
            "pixel_format": info.pixel_format,
            "bit_depth": info.bit_depth,
            "plane_count": info.plane_count,
        },
        "capabilities": {
            "file_version": [caps.file_version_major, caps.file_version_minor],
            "profiles_mask": caps.profiles_mask,
            "max_coded_dim": caps.max_coded_dim,
            "tile_roi": caps.tile_roi,
        },
        "decoded_plane_sizes": [[int(p.shape[1]), int(p.shape[0])] for p in planes],
        "decode": extra,
        "preview": {
            "target": list(preview_target),
            "plane_sizes": [[int(p.shape[1]), int(p.shape[0])]
                            for p in preview_planes],
            "decode": preview_extra,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="a valid .toos file")
    parser.add_argument("--library", help="explicit libtopos_codec path")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.input.is_file():
        parser.error(f"input does not exist: {args.input}")
    payload = json.dumps(run(args.input.resolve(), args.library),
                         ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
