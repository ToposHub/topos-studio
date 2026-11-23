"""Rec.709 Gamma 2.4 model measured from Resolve's HSL curves."""

from __future__ import annotations

from typing import Any

import numpy as np

HSL_CURVE_MODES = ("LumSat", "HueHue", "HueSat", "HueLum", "SatSat", "SatLum")
CALIBRATED_HSL_SAMPLES = 512
_LUMA_WEIGHTS = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


def is_calibrated_hsl_lut(lut: np.ndarray) -> bool:
    return lut.shape == (len(HSL_CURVE_MODES), CALIBRATED_HSL_SAMPLES)


def calibrated_hsl_curve_values(
    points: Any, positions: np.ndarray, *, cyclic: bool = False,
) -> np.ndarray:
    """Sample one Topos curve in 0–255 UI coordinates with flat knot tangents."""
    if not isinstance(points, (list, tuple)):
        return np.full_like(positions, 127.5, dtype=np.float32)
    knots = {}
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            continue
        try:
            x, y = map(float, point)
        except (TypeError, ValueError, OverflowError):
            continue
        if np.isfinite(x) and np.isfinite(y) and 0.0 <= x <= 255.0:
            knots.setdefault(x, np.clip(y, 0.0, 255.0))
    if cyclic and 255.0 in knots:
        last = knots.pop(255.0)
        knots[0.0] = 0.5 * (knots.get(0.0, last) + last)
    if not knots:
        return np.full_like(positions, 127.5, dtype=np.float32)
    if len(knots) == 1:
        return np.full_like(positions, next(iter(knots.values())), dtype=np.float32)
    # Project coordinates remain double during interval selection so two very
    # nearby control points restored from JSON cannot collapse into one knot.
    xs = np.array(sorted(knots), dtype=np.float64)
    ys = np.array([knots[x] for x in xs], dtype=np.float32)
    if cyclic:
        xs = np.concatenate((xs[-1:] - 255.0, xs, xs[:1] + 255.0))
        ys = np.concatenate((ys[-1:], ys, ys[:1]))
    left = np.clip(np.searchsorted(xs, positions, side="right") - 1, 0, len(xs) - 2)
    right = left + 1
    t = np.clip((positions - xs[left]) / (xs[right] - xs[left]), 0.0, 1.0)
    # The measured local Hue/Sat adjustment has a slower falloff at its
    # flat anchors than PCHIP, with a symmetric transition in each interval.
    t2 = np.square(t)
    weight = t2 / (t2 + np.square(1.0 - t))
    return (ys[left] + weight * (ys[right] - ys[left])).astype(np.float32)


def calibrated_hsl_curve_samples(points: Any, mode: str) -> np.ndarray:
    """Return the rendered curve samples in the panel's normalized units."""
    positions = np.linspace(0.0, 255.0, CALIBRATED_HSL_SAMPLES)
    ys = calibrated_hsl_curve_values(points, positions, cyclic=mode.startswith("Hue"))
    return ((127.5 - ys) / 255.0 if mode == "HueHue"
            else (255.0 - ys) / 127.5)


def build_calibrated_hsl_lut(data: dict) -> np.ndarray:
    return np.stack([
        calibrated_hsl_curve_samples(data.get(mode), mode)
        for mode in HSL_CURVE_MODES
    ])


def calibrated_hsl_luma_chroma(encoded: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return luma and the fitted saturation axis from Gamma 2.4 RGB."""
    luma = encoded @ _LUMA_WEIGHTS
    cb = (encoded[..., 2] - luma) / 1.8556
    cr = (encoded[..., 0] - luma) / 1.5748
    chroma = np.hypot(1.87 * cb, 1.815 * cr - 0.418 * cb)
    return luma, chroma


def calibrated_hsl_input(rgb_encoded_0_to_1: Any, axis: str) -> float:
    """Match the calibrated graph input when sampling a viewer color."""
    from colorsys import rgb_to_hsv

    encoded = np.asarray(rgb_encoded_0_to_1, dtype=np.float32)
    if axis == "hue":
        return float(rgb_to_hsv(*encoded)[0])
    luma, chroma = calibrated_hsl_luma_chroma(encoded)
    return float(np.clip(2.0 * chroma if axis == "sat" else luma, 0.0, 1.0))


def _response_envelope(t: np.ndarray, a: float, b: float, c: float) -> np.ndarray:
    """Fit through zero and one with no knee at the upper endpoint."""
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t) + t * (1.0 - t) ** 2 * (a + t * (b + c * t))


def _center_response(value: np.ndarray) -> np.ndarray:
    return _response_envelope(2.0 * np.minimum(value, 1.0 - value), 0.74, 0.86, 1.37)


def apply_calibrated_hsl_curves(rgb: np.ndarray, lut: np.ndarray) -> np.ndarray:
    """Apply six curve responses in encoded RGB, preserving extended range."""
    from src.features.color.core.advanced_grade_cpu import _hsv_to_rgb, _rgb_to_hsv

    encoded = np.copysign(np.power(np.abs(rgb), 1.0 / 2.4), rgb)
    luma, chroma = calibrated_hsl_luma_chroma(encoded)
    hue = _rgb_to_hsv(encoded - encoded.min(axis=-1, keepdims=True))[..., 0]
    positions = np.linspace(0.0, 1.0, CALIBRATED_HSL_SAMPLES)

    def sample(index: int, x: np.ndarray) -> np.ndarray:
        return np.interp(x, positions, lut[index]).astype(np.float32)

    sat_x = 2.0 * chroma
    lum_sat = 1.0 + (sample(0, luma) - 1.0) * _center_response(luma)
    hue_shift = sample(1, hue)
    hue_sat = sample(2, hue)
    hue_lum = (sample(3, hue) - 1.0) * _response_envelope(sat_x, 0.08, -0.44, 1.06)
    sat_response = _center_response(chroma)
    sat_sat = 1.0 + (sample(4, sat_x) - 1.0) * sat_response
    sat_lum = (sample(5, sat_x) - 1.0) * sat_response

    sat_factor = lum_sat * hue_sat * sat_sat
    rotated = _hsv_to_rgb(np.stack([
        np.mod(hue - hue_shift, 1.0),
        np.ones_like(hue),
        np.ptp(encoded, axis=-1),
    ], axis=-1))
    rotated_luma = rotated @ _LUMA_WEIGHTS
    color_delta = np.where(
        (hue_shift != 0.0)[..., None],
        rotated - rotated_luma[..., None],
        encoded - luma[..., None],
    )
    mapped = color_delta * sat_factor[..., None] + (
        luma * (1.0 + hue_lum + sat_lum))[..., None]
    affected = (sat_factor != 1.0) | (hue_shift != 0.0) | (hue_lum + sat_lum != 0.0)
    linear = np.copysign(np.power(np.abs(mapped), 2.4), mapped)
    return np.where(affected[..., None], linear, rgb).astype(np.float32)
