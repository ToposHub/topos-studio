#!/usr/bin/env python3
"""Generate deterministic reduced-decode image-corpus inputs.

The default output is a compact 256x144 smoke corpus.  ``--sizes core`` emits
the existing 1080p/2K/4K shapes; ``--sizes reduced`` emits the high-resolution
shapes used by the 1/2, 1/3, 1/4 and 1/8 preview paths.  ``--sizes all`` emits
both groups.  Large files are intentionally generated on demand and are not
checked in.  Every generated file is hashed in a manifest beside the output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import cv2

CORE_SIZES = {
    "1080p": (1920, 1080),
    "2K": (2048, 1080),
    # Keep the historical UHD name and geometry stable for existing tests.
    "4K": (3840, 2160),
}
REDUCED_SIZES = {
    "4K_DCI": (4096, 2160),
    "6K": (6144, 3240),
    "8K": (7680, 4320),
    "8K_DCI": (8192, 4320),
    "12K": (12288, 6480),
    "16K": (16384, 8640),
}
SIZES = {**CORE_SIZES, **REDUCED_SIZES, "smoke": (256, 144)}
CATEGORIES = (
    "natural_proxy", "gradient", "fine_lines", "text", "texture", "noise",
    "hdr_highlight", "transparent_edges", "composite", "animation",
)


def make(category: str, width: int, height: int, frame: int = 0) -> np.ndarray:
    y, x = np.mgrid[0:height, 0:width]
    if category == "natural_proxy":
        # Deterministic photographic proxy: broad correlated colour fields
        # plus low-amplitude grain.  It is deliberately generated rather than
        # downloaded so the corpus has no media licensing dependency.
        rng = np.random.default_rng(0x4E415455 + frame)
        base = (
            0.45 * np.sin(x / 127.0 + frame * 0.03)
            + 0.35 * np.cos(y / 89.0)
            + 0.20 * np.sin((x + y) / 211.0)
        )
        grain = rng.normal(0.0, 0.025, (height, width))
        r = np.clip((0.52 + base * 0.25 + grain) * 65535.0, 0, 65535).astype(np.uint16)
        g = np.clip((0.42 + base * 0.20 + grain * 0.8) * 65535.0, 0, 65535).astype(np.uint16)
        b = np.clip((0.30 + base * 0.18 + grain * 0.6) * 65535.0, 0, 65535).astype(np.uint16)
    elif category == "gradient":
        r = (x * 65535 // max(1, width - 1)).astype(np.uint16)
        g = (y * 65535 // max(1, height - 1)).astype(np.uint16)
        b = (((x + y + frame * 17) * 65535) // max(1, width + height - 2)).astype(np.uint16)
    elif category == "fine_lines":
        p = (((x // 3 + y // 5 + frame) & 1) * 65535).astype(np.uint16)
        r, g, b = p, np.roll(p, 1, axis=1), np.roll(p, 2, axis=0)
    elif category == "texture":
        p = ((x * 1103515245 + y * 12345 + frame * 2654435761) & 0xFFFF).astype(np.uint16)
        r, g, b = p, np.roll(p, 7, axis=1), np.roll(p, 11, axis=0)
    elif category == "noise":
        rng = np.random.default_rng(0x544F504F + frame)
        r, g, b = [rng.integers(0, 65536, (height, width), dtype=np.uint16) for _ in range(3)]
    elif category == "hdr_highlight":
        base = np.full((height, width), 4096, np.uint16)
        radius = max(1, min(width, height) // 5)
        mask = (x - width // 2) ** 2 + (y - height // 2) ** 2 < radius ** 2
        base[mask] = 65535
        r, g, b = base, np.clip(base * 0.9, 0, 65535).astype(np.uint16), np.clip(base * 0.7, 0, 65535).astype(np.uint16)
    elif category == "transparent_edges":
        r = np.full((height, width), 50000, np.uint16)
        g = np.full((height, width), 12000, np.uint16)
        b = np.full((height, width), 40000, np.uint16)
    elif category in {"text", "composite", "animation"}:
        r = ((x + frame * 9) * 65535 // max(1, width - 1)).astype(np.uint16)
        g = ((y + frame * 5) * 65535 // max(1, height - 1)).astype(np.uint16)
        b = np.bitwise_xor(r, g)
    else:
        raise ValueError(f"unknown corpus category: {category}")
    rgba = np.stack([r, g, b], axis=-1)
    if category == "transparent_edges":
        alpha = np.clip((x - width // 5) * 65535 // max(1, width * 3 // 5), 0, 65535).astype(np.uint16)
        rgba = np.concatenate([rgba, alpha[..., None]], axis=-1)
    return rgba


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("tests/fixtures/topos_image/generated"))
    parser.add_argument("--sizes", choices=("smoke", "core", "reduced", "all"), default="smoke")
    parser.add_argument("--frames", type=int, default=1)
    parser.add_argument("--write-manifest", action="store_true")
    args = parser.parse_args()
    if args.frames < 1:
        parser.error("--frames must be at least 1")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.sizes == "smoke":
        sizes = {"smoke": SIZES["smoke"]}
    elif args.sizes == "core":
        sizes = CORE_SIZES
    elif args.sizes == "reduced":
        sizes = REDUCED_SIZES
    else:
        sizes = {**CORE_SIZES, **REDUCED_SIZES}
    records = []
    for size_name, (width, height) in sizes.items():
        for category in CATEGORIES:
            count = args.frames if category == "animation" else 1
            for frame in range(count):
                path = args.output_dir / f"{category}_{size_name}_{frame:04d}.png"
                rgba = make(category, width, height, frame)
                # OpenCV is used here because Pillow's generic fromarray path
                # does not write uint16 RGB/RGBA PNG arrays portably.
                bgr = rgba[..., [2, 1, 0]]
                if rgba.shape[-1] == 4:
                    bgr = rgba[..., [2, 1, 0, 3]]
                if not cv2.imwrite(str(path), bgr):
                    raise RuntimeError(f"failed to write corpus file: {path}")
                records.append({"path": str(path), "category": category, "size": size_name,
                                "width": width, "height": height, "frame": frame,
                                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    if args.write_manifest:
        # Keep the checked-in contract manifest immutable.  A generated
        # manifest belongs to the output directory and carries concrete file
        # hashes for this run.
        manifest_path = args.output_dir / "manifest.generated.json"
        manifest = json.loads(Path(__file__).with_name("manifest.json").read_text(encoding="utf-8"))
        manifest["generated_files"] = records
        manifest["generated_with"] = {"sizes": args.sizes, "frames": args.frames}
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"files": len(records), "output_dir": str(args.output_dir), "records": records}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
