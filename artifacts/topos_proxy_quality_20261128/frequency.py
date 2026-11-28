#!/usr/bin/env python3
"""Measure preservation of source-correlated luma DCT detail in sampled blocks."""
from __future__ import annotations

import json

import cv2
import numpy as np

from measure import OUT, SOURCES, av_frames, source_path
from perceptual import candidate, spans

BASIS = cv2.dct(np.eye(8, dtype=np.float32), flags=cv2.DCT_ROWS).T
FREQ = np.add.outer(np.arange(8), np.arange(8))
BANDS = {"low_1_2": (FREQ >= 1) & (FREQ <= 2),
         "mid_3_6": (FREQ >= 3) & (FREQ <= 6),
         "high_7_14": FREQ >= 7}


def coefficients(planes):
    y = planes[0]
    h, w = y.shape
    blocks = y.reshape(h//8, 8, w//8, 8)[::4, :, ::4, :].transpose(0, 2, 1, 3)
    return BASIS @ blocks.astype(np.float32) @ BASIS.T


def run(clip: str, tag: str, windows: list[tuple[int, int]]) -> dict:
    _, fps, _ = SOURCES[clip]
    totals = {band: np.zeros(3) for band in BANDS}
    for (_, ref), (_, output) in zip(av_frames(source_path(clip), windows, fps),
                                     candidate(clip, tag, windows), strict=True):
        a, b = coefficients(ref), coefficients(output)
        for band, mask in BANDS.items():
            x, y = a[..., mask], b[..., mask]
            totals[band] += np.array([
                np.sum(x*y, dtype=np.float64), np.sum(x*x, dtype=np.float64),
                np.sum((x-y)**2, dtype=np.float64),
            ])
    return {band: {"correlated_amplitude": float(xy/xx),
                   "normalized_squared_error": float(e2/xx)}
            for band, (xy, xx, e2) in totals.items()}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clip", choices=SOURCES)
    parser.add_argument("tags", nargs="+")
    args = parser.parse_args()
    results = {}
    for tag in args.tags:
        results[tag] = run(args.clip, tag, [(start + 4, 8) for start, _ in spans(args.clip)])
        print(args.clip, tag, results[tag], flush=True)
    (OUT / f"{args.clip}.frequency.json").write_text(json.dumps(results, indent=2) + "\n")
