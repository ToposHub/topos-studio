#!/usr/bin/env python3
"""Measure image quality against a reference using deterministic scalar metrics.

Inputs are PNG/TIFF files readable by OpenCV.  RGB channel order is normalized
explicitly; alpha is measured separately.  The report is intentionally
reference-oriented and does not label a profile lossless unless the decoded
samples are bit-exact.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import cv2
import numpy as np


def _read(path: Path) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise SystemExit(f"could not read image: {path}")
    if image.ndim == 2:
        image = image[..., None]
    if image.shape[-1] == 3:
        image = image[..., [2, 1, 0]]
    elif image.shape[-1] == 4:
        image = image[..., [2, 1, 0, 3]]
    else:
        raise SystemExit(f"expected gray/RGB/RGBA image, got {image.shape}: {path}")
    return image


def _normalized(image: np.ndarray) -> np.ndarray:
    if image.dtype == np.uint8:
        return image.astype(np.float64) / 255.0
    if image.dtype == np.uint16:
        return image.astype(np.float64) / 65535.0
    return image.astype(np.float64)


def _ssim(reference: np.ndarray, candidate: np.ndarray) -> float:
    values = []
    for channel in range(reference.shape[-1]):
        x, y = reference[..., channel], candidate[..., channel]
        mux, muy = float(x.mean()), float(y.mean())
        vx, vy = float(x.var()), float(y.var())
        covariance = float(((x - mux) * (y - muy)).mean())
        c1, c2 = 0.01 ** 2, 0.03 ** 2
        values.append(((2 * mux * muy + c1) * (2 * covariance + c2)) /
                      ((mux * mux + muy * muy + c1) * (vx + vy + c2)))
    return float(np.mean(values))


def _delta_e_2000(reference_rgb: np.ndarray, candidate_rgb: np.ndarray) -> tuple[float, float]:
    ref_lab = cv2.cvtColor(reference_rgb.astype(np.float32), cv2.COLOR_RGB2LAB).astype(np.float64)
    got_lab = cv2.cvtColor(candidate_rgb.astype(np.float32), cv2.COLOR_RGB2LAB).astype(np.float64)
    l1, a1, b1 = [ref_lab[..., i] for i in range(3)]
    l2, a2, b2 = [got_lab[..., i] for i in range(3)]
    c1 = np.hypot(a1, b1)
    c2 = np.hypot(a2, b2)
    c_bar = (c1 + c2) / 2.0
    g = 0.5 * (1.0 - np.sqrt((c_bar ** 7) / (c_bar ** 7 + 25.0 ** 7)))
    ap1, ap2 = (1.0 + g) * a1, (1.0 + g) * a2
    cp1, cp2 = np.hypot(ap1, b1), np.hypot(ap2, b2)
    hp1 = (np.degrees(np.arctan2(b1, ap1)) + 360.0) % 360.0
    hp2 = (np.degrees(np.arctan2(b2, ap2)) + 360.0) % 360.0
    dl, dc = l2 - l1, cp2 - cp1
    dh = hp2 - hp1
    dh = np.where(dh > 180.0, dh - 360.0, dh)
    dh = np.where(dh < -180.0, dh + 360.0, dh)
    dh_term = 2.0 * np.sqrt(cp1 * cp2) * np.sin(np.radians(dh) / 2.0)
    l_bar, c_bar_p = (l1 + l2) / 2.0, (cp1 + cp2) / 2.0
    h_bar = np.where(np.abs(hp1 - hp2) <= 180.0, (hp1 + hp2) / 2.0,
                     np.where(hp1 + hp2 < 360.0, (hp1 + hp2 + 360.0) / 2.0,
                              (hp1 + hp2 - 360.0) / 2.0))
    t = (1.0 - 0.17 * np.cos(np.radians(h_bar - 30.0))
         + 0.24 * np.cos(np.radians(2.0 * h_bar))
         + 0.32 * np.cos(np.radians(3.0 * h_bar + 6.0))
         - 0.20 * np.cos(np.radians(4.0 * h_bar - 63.0)))
    sl = 1.0 + 0.015 * (l_bar - 50.0) ** 2 / np.sqrt(20.0 + (l_bar - 50.0) ** 2)
    sc = 1.0 + 0.045 * c_bar_p
    sh = 1.0 + 0.015 * c_bar_p * t
    rt = -2.0 * np.sqrt((c_bar_p ** 7) / (c_bar_p ** 7 + 25.0 ** 7)) * np.sin(
        np.radians(60.0 * np.exp(-((h_bar - 275.0) / 25.0) ** 2)))
    delta = np.sqrt((dl / sl) ** 2 + (dc / sc) ** 2 + (dh_term / sh) ** 2
                    + rt * (dc / sc) * (dh_term / sh))
    return float(delta.mean()), float(delta.max())


def measure(reference: np.ndarray, candidate: np.ndarray) -> dict[str, Any]:
    if reference.shape != candidate.shape:
        raise SystemExit(f"shape mismatch: reference={reference.shape}, candidate={candidate.shape}")
    ref = _normalized(reference)
    got = _normalized(candidate)
    diff = got - ref
    mse = float(np.mean(diff ** 2))
    peak = 1.0
    # JSON has no portable Infinity literal.  ``None`` plus bit_exact=true is
    # the machine-readable representation of mathematically infinite PSNR.
    psnr: float | None = None if mse == 0.0 else 10.0 * math.log10(peak * peak / mse)
    rgb_channels = min(3, ref.shape[-1])
    rgb_ref, rgb_got = ref[..., :rgb_channels], got[..., :rgb_channels]
    result: dict[str, Any] = {
        "shape": list(reference.shape),
        "bit_exact": bool(np.array_equal(reference, candidate)),
        "psnr_db": psnr,
        "ssim": _ssim(rgb_ref, rgb_got),
        "max_abs_error": float(np.max(np.abs(diff))),
        "mean_signed_error": float(np.mean(diff)),
        "rgb_max_abs_error": float(np.max(np.abs(rgb_got - rgb_ref))),
    }
    if rgb_channels == 3:
        result["delta_e2000_mean"] , result["delta_e2000_max"] = _delta_e_2000(rgb_ref, rgb_got)
    if ref.shape[-1] == 4:
        alpha_diff = got[..., 3] - ref[..., 3]
        result["alpha"] = {"max_abs_error": float(np.max(np.abs(alpha_diff))),
                           "mean_signed_error": float(np.mean(alpha_diff)),
                           "bit_exact": bool(np.array_equal(reference[..., 3], candidate[..., 3]))}
    result["quality_class"] = "lossless" if result["bit_exact"] else "measured_lossy_or_quantized"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = {"schema_version": 1, "reference": str(args.reference.resolve()),
              "candidate": str(args.candidate.resolve()),
              "metrics": measure(_read(args.reference), _read(args.candidate))}
    payload = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
