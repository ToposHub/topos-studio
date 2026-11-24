"""Build the 709 Color Warper response profiles from 16-bit Resolve TIFF renders.

Use the Rec.709 Gamma 2.4, Full chart from resolve_primary_calibration. Each
capture folder contains one TIFF and a warper.json with one single-axis edit.
The independent RGB patches and gray ramp are reserved for validation.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from src.features.color.core.warper_color import _hs709_basis
from src.features.color.core.warper_response import (
    HUE_INTERVALS, PEAK_LEVELS, PEAK_MAX, PEAK_MIN, PROFILE_HALF_WIDTH, SAT_INTERVALS,
)
from tools.resolve_primary_calibration import GRID, MAX_CODE, chart_codes, read_patches


CAPTURES = (
    "hsp-red-hue", "v2-red-hue-reverse", "hsp-red-sat", "hsp-red-luma",
    "v2-cyan-hue", "v2-cyan-sat", "v2-magenta-inner-hue",
)
VALIDATION_CAPTURES = ("hsp-cyan-mixed", "v2-red-hue-small", "v2-blue-outer-mixed", "v2-green-inner-mixed")
_SAT_DISPLAY_SCALE = 2.0 / np.sqrt(3.0)


def _capture(path: Path) -> np.ndarray:
    frames = list(path.glob("*.tif"))
    if len(frames) != 1:
        raise ValueError(f"Expected one TIFF render in {path}")
    return read_patches(frames[0])


def _moves(path: Path) -> list[tuple[float, float, tuple[float, float, float]]]:
    edits = json.loads((path / "warper.json").read_text(encoding="utf-8"))
    moves = []
    for e in edits:
        turn, ring = e["col"] / 6.0, e["row"] / 6.0
        if not (0.0 <= turn < 1.0 and 0.0 < ring <= 1.0):
            raise ValueError(f"Invalid mesh coordinates in {path}")
        hue = (-turn) % 1.0
        delta_hue = (turn - e["hue"] + 0.5) % 1.0 - 0.5
        delta_sat = 0.0 if e["sat"] is None else e["sat"] / _SAT_DISPLAY_SCALE - ring
        moves.append((hue, ring, (delta_hue, delta_sat, e["luma"] - 0.5)))
    return moves


def _control(path: Path) -> tuple[int, float, float, float]:
    moves = _moves(path)
    if len(moves) != 1:
        raise ValueError(f"Expected one 6×6 mesh edit in {path}")
    hue, ring, values = moves[0]
    axes = [i for i, value in enumerate(values) if abs(value) > 1.0e-8]
    if len(axes) != 1:
        raise ValueError(f"Expected one changed axis in {path}")
    return axes[0], hue, ring, values[axes[0]]


def _profile(delta: np.ndarray, hue: float) -> np.ndarray:
    from scipy.interpolate import RegularGridInterpolator

    coordinates = np.linspace(0.0, 1.0, GRID)
    cube = delta[:GRID**3].reshape((GRID,) * 3 + (3,)) / MAX_CODE
    # The chart stores R fastest, so the interpolator's indices are B, G, R.
    interpolate = RegularGridInterpolator((coordinates,) * 3, cube)
    angles = hue + np.arange(-PROFILE_HALF_WIDTH, PROFILE_HALF_WIDTH + 1) / HUE_INTERVALS
    sat = np.linspace(0.0, 1.0, SAT_INTERVALS + 1)[None, :, None, None]
    peak = np.linspace(PEAK_MIN, PEAK_MAX, PEAK_LEVELS)[:, None, None, None]
    source = (1.0 - sat + sat * _hs709_basis(angles)[None, None, :, :]) * peak
    response = interpolate(source[..., ::-1]) / peak
    response[:, 0] = 0.0
    response[:, :, [0, -1]] = 0.0
    return response.astype(np.float16)


def build(captures: Path, output: Path) -> None:
    neutral = _capture(captures / "neutral")
    if np.max(np.abs(neutral.astype(np.int32) - chart_codes())) > 2:
        raise ValueError("Neutral TIFF does not match the full-range chart")
    controls, responses = [], []
    for case in CAPTURES:
        path = captures / case
        axis, hue, ring, delta = _control(path)
        response = _profile(_capture(path).astype(np.int32) - neutral, hue)
        controls.append((axis, hue, ring, delta))
        responses.append(response)
    with output.open("xb") as stream:
        np.savez_compressed(stream, controls=np.array(controls), responses=np.stack(responses))


def write_validation(captures: Path, output: Path) -> None:
    """Save independent patches, including settings omitted from profile generation."""
    cases = (*CAPTURES, *VALIDATION_CAPTURES)
    holdout = slice(GRID**3, None)
    before = _capture(captures / "neutral")[holdout]
    targets, meshes = [], []
    for case in cases:
        folder = captures / case
        targets.append(_capture(folder)[holdout])
        offsets = np.zeros((6, 6, 3), np.float32)
        for hue, radius, values in _moves(folder):
            offsets[round(radius * 6) - 1, round(hue * 6) % 6] = values
        meshes.append(offsets)
    with output.open("xb") as stream:
        np.savez_compressed(stream, cases=cases, before=before, targets=targets, offsets=meshes)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--validation", type=Path)
    args = parser.parse_args()
    build(args.captures, args.output)
    if args.validation is not None:
        write_validation(args.captures, args.validation)
