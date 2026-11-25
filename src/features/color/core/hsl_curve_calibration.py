"""Rec.709 Gamma 2.4 model measured from Resolve's HSL curves."""

from __future__ import annotations

from typing import Any

import numpy as np

from src.features.color.core.curves_calibration import HSL_CURVES_CALIBRATION_VERSION

HSL_CURVE_MODES = ("LumSat", "HueHue", "HueSat", "HueLum", "SatSat", "SatLum")
CALIBRATED_HSL_SAMPLES = 512
HSL_V3_SAMPLES = 1024
VALID_HSL_LUT_SHAPES = ((6, 256), (6, CALIBRATED_HSL_SAMPLES), (6, HSL_V3_SAMPLES))
_LUMA_WEIGHTS = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


def is_calibrated_hsl_lut(lut: np.ndarray) -> bool:
    return lut.shape in VALID_HSL_LUT_SHAPES[1:]


def hsl_lut_shape(version: int) -> tuple[int, int]:
    if version == HSL_CURVES_CALIBRATION_VERSION:
        return VALID_HSL_LUT_SHAPES[2]
    return VALID_HSL_LUT_SHAPES[1 if version else 0]


def hsl_control_knots(points: Any) -> dict[float, float]:
    """Read finite control points in the editor's coordinate range."""
    if not isinstance(points, (list, tuple)):
        return {}
    knots: dict[float, float] = {}
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            continue
        try:
            x, y = map(float, point)
        except (TypeError, ValueError, OverflowError):
            continue
        if np.isfinite(x) and np.isfinite(y) and 0.0 <= x <= 255.0:
            knots.setdefault(x, min(max(y, 0.0), 255.0))
    return knots


def calibrated_hsl_curve_values(
    points: Any, positions: np.ndarray, *, cyclic: bool = False, version: int = 1,
) -> np.ndarray:
    """Sample the authored curve in 0–255 UI coordinates."""
    knots = hsl_control_knots(points)
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
    if version == HSL_CURVES_CALIBRATION_VERSION:
        return _hsl_v3_curve_values(xs, ys, positions, cyclic=cyclic)
    left = np.clip(np.searchsorted(xs, positions, side="right") - 1, 0, len(xs) - 2)
    right = left + 1
    t = np.clip((positions - xs[left]) / (xs[right] - xs[left]), 0.0, 1.0)
    # The measured local Hue/Sat adjustment has a slower falloff at its
    # flat anchors than PCHIP, with a symmetric transition in each interval.
    t2 = np.square(t)
    weight = t2 / (t2 + np.square(1.0 - t))
    return (ys[left] + weight * (ys[right] - ys[left])).astype(np.float32)


def _hsl_v3_curve_values(
    xs: np.ndarray, ys: np.ndarray, positions: np.ndarray, *, cyclic: bool,
) -> np.ndarray:
    """Use half-segment Bézier handles, horizontal at slope reversals."""
    widths = np.diff(xs)
    slopes = np.diff(ys) / widths
    tangent = np.empty_like(xs)
    continued = slopes[:-1] * slopes[1:] > 0.0
    tangent[1:-1] = np.where(continued, (ys[2:] - ys[:-2]) / (xs[2:] - xs[:-2]), 0.0)
    if cyclic:
        tangent[0], tangent[-1] = tangent[-2], tangent[1]
    else:
        tangent[0], tangent[-1] = slopes[0], slopes[-1]

    index = np.clip(np.searchsorted(xs, positions, side="right") - 1, 0, len(xs) - 2)
    right = index + 1
    width = widths[index]
    x = np.clip((positions - xs[index]) / width, 0.0, 1.0)
    entry = 0.5 / np.hypot(1.0, tangent[index])
    exit = 0.5 / np.hypot(1.0, tangent[right])
    lo, hi = np.zeros_like(x), np.ones_like(x)
    for _ in range(16):
        t = (lo + hi) * 0.5
        s = 1.0 - t
        mid = 3.0 * s * t * (s * entry + t * (1.0 - exit)) + t ** 3
        lo, hi = np.where(mid < x, t, lo), np.where(mid >= x, t, hi)
    t = (lo + hi) * 0.5
    s = 1.0 - t
    start, end = ys[index], ys[right]
    min_y, max_y = np.minimum(start, end), np.maximum(start, end)
    y1 = np.clip(start + width * entry * tangent[index], min_y, max_y)
    y2 = np.clip(end - width * exit * tangent[right], min_y, max_y)
    result = s ** 3 * start + 3.0 * s * t * (s * y1 + t * y2) + t ** 3 * end
    result = np.where(x <= 0.0, start, np.where(x >= 1.0, end, result))
    return result.astype(np.float32)


def calibrated_hsl_curve_samples(points: Any, mode: str, *, version: int = 1) -> np.ndarray:
    """Return the rendered curve samples in the panel's normalized units."""
    positions = np.linspace(0.0, 255.0, hsl_lut_shape(version)[1])
    ys = calibrated_hsl_curve_values(
        points, positions, cyclic=mode.startswith("Hue"), version=version)
    return ((127.5 - ys) / 255.0 if mode == "HueHue"
            else (255.0 - ys) / 127.5)


def build_calibrated_hsl_lut(data: dict, *, version: int = 1) -> np.ndarray:
    return np.stack([
        calibrated_hsl_curve_samples(data.get(mode), mode, version=version)
        for mode in HSL_CURVE_MODES
    ])


def calibrated_hsl_luma_chroma(encoded: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return luma and the fitted saturation axis from Gamma 2.4 RGB."""
    luma = encoded @ _LUMA_WEIGHTS
    cb = (encoded[..., 2] - luma) / 1.8556
    cr = (encoded[..., 0] - luma) / 1.5748
    chroma = np.hypot(1.87 * cb, 1.815 * cr - 0.418 * cb)
    return luma, chroma


def _hsl_v3_chroma(encoded: np.ndarray, luma: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    cb = (encoded[..., 2] - luma) / 1.8556
    cr = (encoded[..., 0] - luma) / 1.5748
    u, v = 1.816 * cr - 0.41838 * cb, 1.87 * cb
    return u, v, 1.02076 * np.hypot(u, v)


def _sat_v3_axis(chroma: np.ndarray) -> np.ndarray:
    return (1023.0 * chroma - 1.0) / 1024.0


def _hue_v3_axis(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    # The curve begins at red in the same chroma plane as the rotation.
    phase = np.mod(-0.03509087 - np.arctan2(v, u) / (2.0 * np.pi), 1.0)
    return np.mod((1023.0 / 1024.0) * phase - 1.0 / 2048.0, 1.0)


def _is_hsl_red_seam(encoded: np.ndarray) -> np.ndarray:
    return (encoded[..., 0] > encoded[..., 1]) & (encoded[..., 1] == encoded[..., 2])


def calibrated_hsl_input(rgb_encoded_0_to_1: Any, axis: str, *, version: int = 1) -> float:
    """Match the calibrated graph input when sampling a viewer color."""
    from colorsys import rgb_to_hsv

    encoded = np.asarray(rgb_encoded_0_to_1, dtype=np.float32)
    if version == HSL_CURVES_CALIBRATION_VERSION:
        luma = encoded @ _LUMA_WEIGHTS
        u, v, chroma = _hsl_v3_chroma(encoded, luma)
        if axis == "hue":
            return float(_hue_v3_axis(u, v)) if chroma > 1.0e-7 and not _is_hsl_red_seam(encoded) else 0.0
        axis_value = _sat_v3_axis(chroma) if axis == "sat" else (1023.0 * luma - 0.5) / 1024.0
        return float(np.clip(axis_value, 0.0, 1.0))
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


def _apply_hsl_v3(encoded: np.ndarray, lut: np.ndarray) -> np.ndarray:
    from src.features.color.core.advanced_grade_cpu import _rgb_to_hsv

    luma = encoded @ _LUMA_WEIGHTS
    u, v, chroma = _hsl_v3_chroma(encoded, luma)
    red_seam = _is_hsl_red_seam(encoded)
    hue = np.where((chroma > 1.0e-7) & ~red_seam, _hue_v3_axis(u, v), 0.0)
    positions = np.linspace(0.0, 1.0, HSL_V3_SAMPLES)

    def sample(index: int, x: np.ndarray) -> np.ndarray:
        return np.interp(x, positions, lut[index]).astype(np.float32)

    sat_x = _sat_v3_axis(chroma)
    luma_x = (1023.0 * luma - 0.5) / 1024.0
    axis_sat = sample(0, luma_x) * sample(4, sat_x)
    sat = axis_sat * sample(2, hue)
    sat_lum = sample(5, sat_x)
    # Hue/Lum's attenuation sees the luminance- and saturation-selected
    # saturation changes. Hue/Sat contributes only to the output chroma.
    hue_lum_t = np.clip(2.0 * chroma * axis_sat, 0.0, 1.0)
    hue_lum = (sample(3, hue) - 1.0) * hue_lum_t ** 2 * (3.0 - 2.0 * hue_lum_t)
    rotation = sample(1, hue)
    delta = encoded - luma[..., None]
    if np.any(rotation != 0.0):
        hsv_hue = _rgb_to_hsv(encoded - encoded.min(axis=-1, keepdims=True))[..., 0]
        drift = np.where(red_seam, 0.0, (hsv_hue + 0.5) / 1024.0)
        theta = (rotation + drift) * (2.0 * np.pi)
        cos_theta, sin_theta = np.cos(theta), np.sin(theta)
        cb = (u * sin_theta + v * cos_theta) / 1.87
        cr = (u * cos_theta - v * sin_theta + 0.41838 * cb) / 1.816
        red, blue = 1.5748 * cr, 1.8556 * cb
        green = -(0.2126 * red + 0.0722 * blue) / 0.7152
        rotated = np.stack((red, green, blue), axis=-1)
        delta = np.where((rotation != 0.0)[..., None], rotated, delta)
    lum_factor = (1.0 + hue_lum) * sat_lum
    mapped = delta * sat[..., None] + (luma * lum_factor)[..., None]
    active = (rotation != 0.0) | (sat != 1.0) | (lum_factor != 1.0)
    return np.where(active[..., None], mapped, encoded)


def apply_calibrated_hsl_curves(
    rgb: np.ndarray, lut: np.ndarray, *, encoded_input: bool = False,
) -> np.ndarray:
    """Apply six curve responses in encoded RGB, preserving extended range."""
    from src.features.color.core.advanced_grade_cpu import _hsv_to_rgb, _rgb_to_hsv

    encoded = rgb if encoded_input else np.copysign(np.power(np.abs(rgb), 1.0 / 2.4), rgb)
    if lut.shape[1] == HSL_V3_SAMPLES:
        mapped = _apply_hsl_v3(encoded, lut)
        if encoded_input:
            return mapped.astype(np.float32)
        linear = np.copysign(np.power(np.abs(mapped), 2.4), mapped)
        return np.where(mapped == encoded, rgb, linear).astype(np.float32)
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
    if not encoded_input:
        mapped = np.copysign(np.power(np.abs(mapped), 2.4), mapped)
    return np.where(affected[..., None], mapped, rgb).astype(np.float32)
