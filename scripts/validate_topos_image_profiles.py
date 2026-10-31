#!/usr/bin/env python3
"""Validate the image profile/tier contract in capability_manifest.json."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def validate(path: Path) -> None:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    profiles = manifest["profiles"]
    # TRAW（2026-09-13 拍板）：reserved 值 3（Image Lossless）改派 Image RAW——
    # Lossless 档移除，无损语义由 RAW Linear 模式（批 4）承载。
    if profiles["3"]["status"] != "supported" or profiles["3"]["codec_pixel_format"] != "cfa":
        raise ValueError("image profile 3 must be supported Image RAW (CFA)")
    if manifest["constraints"]["integer_gbr16_image_profile"] is not False:
        raise ValueError("GBR16 UINT image is not a registered product profile")
    for profile in profiles.values():
        if (profile.get("codec_pixel_format") == "gbr"
                and profile.get("sample_kind") is None
                and 16 in profile.get("bit_depths", ())):
            raise ValueError("GBR16 UINT image requires a new registered profile")
    if not profiles["4"]["lossy_tier_nonfinite_policy"].startswith("reject_before_publish"):
        raise ValueError("float-* lossy tiers must reject Inf/NaN before publish")
    assert manifest["constraints"]["tile_roi"] is False
    tiers = manifest["tiers"]
    expected = ("422-low", "422-medium", "422-high", "422-ultra")
    sizes = [tiers[name]["anchor_mb_per_frame_1080p"] for name in expected]
    if sizes != sorted(sizes):
        raise ValueError(f"422 tier anchors are not monotonic: {sizes}")
    if tiers["422-low"]["pixel_format"] != 0 or tiers["422-low"]["qmatrix"] != 3:
        raise ValueError("422-low must remain the compact YUV profile")
    if tiers["444-high"]["pixel_format"] != 2 or tiers["444-ultra"]["pixel_format"] != 2:
        raise ValueError("444 tiers must remain GBR")
    for name, tier in tiers.items():
        if name.startswith("$"):
            continue
        for key in ("image_profile", "pixel_format", "bit_depth", "qp", "qmatrix", "anchor_mb_per_frame_1080p"):
            if key not in tier:
                raise ValueError(f"tier {name} missing {key}")
    raw_tiers = ("raw12-2", "raw12-4", "raw12-6", "raw12-8", "raw12-12", "raw12-16",
                 "raw16-2", "raw16-4", "raw16-6", "raw16-8", "raw16-12", "raw16-16")
    ratios = (2, 4, 6, 8, 12, 16, 2, 4, 6, 8, 12, 16)
    container_1080p_mb = 1920 * 1080 * 2 / 1e6  # 16-bit 容器名义口径
    for name, ratio in zip(raw_tiers, ratios):
        tier = tiers[name]
        if tier["image_profile"] != 3 or tier["pixel_format"] != 3 or tier["qmatrix"] != 0:
            raise ValueError(f"{name} must be CFA/TRAW qm0")
        if abs(tier["anchor_mb_per_frame_1080p"] - container_1080p_mb / ratio) > 0.01:
            raise ValueError(f"{name} anchor must equal nominal {ratio}:1 of 16-bit container")
        if name.startswith("raw16-") and tier["bit_depth"] != 16:
            raise ValueError(f"{name} must be 16-bit linear (batch 4)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    validate(args.manifest)
    print(f"valid Topos Image profile contract: {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
