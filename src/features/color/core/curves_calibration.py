"""Rec.709 Gamma 2.4 custom-curve calibration and packed response layout."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

CURVES_CALIBRATION_VERSION = 1
CUSTOM_CURVES_CALIBRATION_VERSION = 2
HSL_CURVES_CALIBRATION_VERSION = 3
CURVES_CALIBRATION_VERSION_KEY = "calibration_version"
CURVE_SAMPLES = 256
# The first half contains RGB deltas. The second holds Y deltas in R.
# Width distinguishes the response texture from the legacy absolute RGB LUT.
CALIBRATED_CURVES_WIDTH = 2 * CURVE_SAMPLES
_UNIT_SAMPLES = np.linspace(0.0, 1.0, CURVE_SAMPLES)
_REC709_LUMA = np.array((0.2126, 0.7152, 0.0722), dtype=np.float32)
_V2_INPUT_SCALE = 31.0 / 32.0
_V2_INPUT_OFFSET = 1.0 / 2048.0


def curves_calibration_version(edit_state: Any) -> int:
    if not isinstance(edit_state, Mapping):
        return 0
    version = edit_state.get(CURVES_CALIBRATION_VERSION_KEY)
    if type(version) is int and version in (
        CURVES_CALIBRATION_VERSION, CUSTOM_CURVES_CALIBRATION_VERSION, HSL_CURVES_CALIBRATION_VERSION,
    ):
        return version
    return 0


def curves_calibration_enabled(edit_state: Any) -> bool:
    return curves_calibration_version(edit_state) != 0


def is_calibrated_curves_lut(lut: np.ndarray) -> bool:
    return lut.shape == (CALIBRATED_CURVES_WIDTH, 1, 3)


def calibrated_curve_samples(
    xs: np.ndarray, ys: np.ndarray, *, version: int = CURVES_CALIBRATION_VERSION,
    for_render: bool = False,
) -> np.ndarray:
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
    if version >= CUSTOM_CURVES_CALIBRATION_VERSION:
        if np.allclose(x, y, rtol=0.0, atol=1e-7):
            return _UNIT_SAMPLES
        domain = _UNIT_SAMPLES * _V2_INPUT_SCALE if for_render else _UNIT_SAMPLES
        # Interior Resolve signal samples are biased by half of a 10-bit step.
        argument = domain - _V2_INPUT_OFFSET if for_render else domain
        mapped = _sample_parametric_curve(x, y, argument)
        return _UNIT_SAMPLES + (mapped - domain)

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


def _sample_parametric_curve(x: np.ndarray, y: np.ndarray, axis: np.ndarray) -> np.ndarray:
    """Evaluate the fitted parametric cubic at evenly spaced input values."""
    if x.size == 2:
        return y[0] + (axis - x[0]) * (y[1] - y[0]) / (x[1] - x[0])
    points = np.stack((x, y), axis=1)
    chords = np.diff(points, axis=0)
    spacing = np.linalg.norm(chords, axis=1)
    velocity = chords / spacing[:, None]
    tangents = np.empty_like(points)
    tangents[0], tangents[-1] = velocity[0], velocity[-1]
    total = spacing[:-1] + spacing[1:]
    tangents[1:-1] = (points[2:] - points[:-2]) / total[:, None]

    # Keep the Bézier X handles ordered even when knots are close together.
    entry = tangents[:-1] * spacing[:, None] / 3.0
    exit = tangents[1:] * spacing[:, None] / 3.0
    limit = np.minimum(1.0, chords[:, 0] / (entry[:, 0] + exit[:, 0]))
    control_in = points[:-1] + entry * limit[:, None]
    control_out = points[1:] - exit * limit[:, None]
    index = np.clip(np.searchsorted(x, axis, side="right") - 1, 0, len(chords) - 1)
    p0, p1 = points[index], control_in[index]
    p2, p3 = control_out[index], points[index + 1]

    lower, upper = np.zeros_like(axis), np.ones_like(axis)
    for _ in range(18):
        t = (lower + upper) * 0.5
        s = 1.0 - t
        position = (s**3 * p0[:, 0] + 3.0 * s**2 * t * p1[:, 0]
                    + 3.0 * s * t**2 * p2[:, 0] + t**3 * p3[:, 0])
        lower = np.where(position < axis, t, lower)
        upper = np.where(position >= axis, t, upper)
    t = (lower + upper) * 0.5
    s = 1.0 - t
    result = s**3 * p0[:, 1] + 3.0 * s**2 * t * p1[:, 1] + 3.0 * s * t**2 * p2[:, 1] + t**3 * p3[:, 1]

    # Retain unclipped changes for RGB luma compensation outside moved endpoints.
    slope = chords[:, 1] / chords[:, 0]
    result = np.where(axis <= x[0], y[0] + (axis - x[0]) * slope[0], result)
    return np.where(axis >= x[-1], y[-1] + (axis - x[-1]) * slope[-1], result)


def build_calibrated_curves_lut(
    points: Mapping[str, tuple[np.ndarray, np.ndarray] | None],
    amounts: Mapping[str, float],
    *, version: int = CURVES_CALIBRATION_VERSION,
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
            samples = calibrated_curve_samples(*control, version=version, for_render=True)
            lut[texels, 0, component] = (samples - _UNIT_SAMPLES) * amounts[channel]
    # The cubic may differ from the identity by a float round-off at its knots.
    lut[np.abs(lut) < 1.0e-7] = 0.0
    return lut


def apply_calibrated_curves_cpu(
    color: np.ndarray, lut: np.ndarray, *, encoded_input: bool = False,
) -> np.ndarray:
    """Apply Y to encoded luma and RGB to chroma in the same color space."""
    rgb = np.asarray(color, dtype=np.float32)
    if not np.any(lut):
        return rgb
    encoded = rgb if encoded_input else np.copysign(np.abs(rgb) ** np.float32(1.0 / 2.4), rgb)
    axis = _UNIT_SAMPLES
    deltas = np.empty_like(encoded)
    for channel in range(3):
        deltas[..., channel] = np.interp(
            encoded[..., channel], axis, lut[:CURVE_SAMPLES, 0, channel])
    luma_delta = np.interp(
        encoded @ _REC709_LUMA, axis, lut[CURVE_SAMPLES:, 0, 0]).astype(np.float32)
    response = deltas + (luma_delta - deltas @ _REC709_LUMA)[..., None]
    graded = encoded + response
    if not encoded_input:
        graded = np.copysign(np.abs(graded) ** np.float32(2.4), graded)
    return np.where(response == 0.0, rgb, graded)
