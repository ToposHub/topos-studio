"""Rec.709 Gamma 2.4 custom-curve calibration and packed response layout."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

CURVES_CALIBRATION_VERSION = 1
CURVES_CALIBRATION_VERSION_KEY = "calibration_version"
CURVE_SAMPLES = 256
# The first half contains RGB deltas. The second holds Y deltas in R.
# Width distinguishes the response texture from the legacy absolute RGB LUT.
CALIBRATED_CURVES_WIDTH = 2 * CURVE_SAMPLES
_UNIT_SAMPLES = np.linspace(0.0, 1.0, CURVE_SAMPLES)
_REC709_LUMA = np.array((0.2126, 0.7152, 0.0722), dtype=np.float32)


def curves_calibration_enabled(edit_state: Any) -> bool:
    if not isinstance(edit_state, Mapping):
        return False
    version = edit_state.get(CURVES_CALIBRATION_VERSION_KEY)
    return type(version) is int and version == CURVES_CALIBRATION_VERSION


def is_calibrated_curves_lut(lut: np.ndarray) -> bool:
    return lut.shape == (CALIBRATED_CURVES_WIDTH, 1, 3)


def calibrated_curve_samples(xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Sample a bounded cubic curve with local secant-based tangents.

    The control point coordinates use the editor's inverted 0–255 Y axis.
    Interior tangents are limited by adjacent segments to avoid overshoot at
    tight point spacing. Moves of the first/last point leave a flat tail.
    """
    x = np.asarray(xs, dtype=np.float64) / 255.0
    y = 1.0 - np.asarray(ys, dtype=np.float64) / 255.0
    valid = np.isfinite(x) & np.isfinite(y)
    x, y = x[valid], y[valid]
    x, first = np.unique(x, return_index=True)
    y = y[first]
    if x.size < 2:
        return _UNIT_SAMPLES

    widths = np.diff(x)
    slopes = np.diff(y) / widths
    tangent = np.empty_like(y)
    tangent[0], tangent[-1] = slopes[0], slopes[-1]
    left, right = slopes[:-1], slopes[1:]
    middle = (left + right) * 0.5
    limit = 3.0 * np.minimum(np.abs(left), np.abs(right))
    tangent[1:-1] = np.where(left * right > 0.0, np.clip(middle, -limit, limit), 0.0)
    segment = np.clip(np.searchsorted(x, _UNIT_SAMPLES, side="right") - 1, 0, x.size - 2)
    width = widths[segment]
    t = np.clip((_UNIT_SAMPLES - x[segment]) / width, 0.0, 1.0)
    s = 1.0 - t
    sampled = (
        s * s * ((1.0 + 2.0 * t) * y[segment] + t * width * tangent[segment])
        + t * t * ((3.0 - 2.0 * t) * y[segment + 1] - s * width * tangent[segment + 1])
    )
    return np.clip(sampled, 0.0, 1.0)


def build_calibrated_curves_lut(
    points: Mapping[str, tuple[np.ndarray, np.ndarray] | None],
    amounts: Mapping[str, float],
) -> np.ndarray:
    lut = np.zeros((CALIBRATED_CURVES_WIDTH, 1, 3), dtype=np.float32)
    for channel, texels, component in (
        ("R", slice(0, CURVE_SAMPLES), 0),
        ("G", slice(0, CURVE_SAMPLES), 1),
        ("B", slice(0, CURVE_SAMPLES), 2),
        ("M", slice(CURVE_SAMPLES, None), 0),
    ):
        control = points.get(channel)
        if control is not None:
            samples = calibrated_curve_samples(*control)
            lut[texels, 0, component] = (samples - _UNIT_SAMPLES) * amounts[channel]
    # The cubic may differ from the identity by a float round-off at its knots.
    lut[np.abs(lut) < 1.0e-7] = 0.0
    return lut


def apply_calibrated_curves_cpu(color: np.ndarray, lut: np.ndarray) -> np.ndarray:
    """Apply Y to encoded luma and RGB to chroma in the same color space."""
    rgb = np.asarray(color, dtype=np.float32)
    if not np.any(lut):
        return rgb
    encoded = np.copysign(np.abs(rgb) ** np.float32(1.0 / 2.4), rgb)
    axis = _UNIT_SAMPLES
    deltas = np.empty_like(encoded)
    for channel in range(3):
        deltas[..., channel] = np.interp(
            encoded[..., channel], axis, lut[:CURVE_SAMPLES, 0, channel])
    luma_delta = np.interp(
        encoded @ _REC709_LUMA, axis, lut[CURVE_SAMPLES:, 0, 0]).astype(np.float32)
    response = deltas + (luma_delta - deltas @ _REC709_LUMA)[..., None]
    graded = encoded + response
    decoded = np.copysign(np.abs(graded) ** np.float32(2.4), graded)
    return np.where(response == 0.0, rgb, decoded)
