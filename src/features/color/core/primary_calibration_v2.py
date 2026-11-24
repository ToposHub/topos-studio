"""Continuous encoded-domain responses fitted to Resolve 20.2 Rec.709 captures."""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from src.features.color.core.primary_calibration import (
    Control, PrimaryCalibration, _GRID_CACHE_SIZE, _readonly_grid,
)

PROFILE_PATH = Path(__file__).with_name("data") / "primary_calibration_rec709_g24_v2.npz"
_LUMA = np.array((0.2126, 0.7152, 0.0722), dtype=np.float32)
_CHROMA_LUMA = np.array((0.21, 0.70, 0.09), dtype=np.float32)
_HUE_SINE = np.array(
    ((0.210000, 0.720000, -0.930000),
     (-0.162171, -0.127449, 0.289620),
     (0.771332, -0.688777, -0.082555)), dtype=np.float32,
)
_LOG_CONTROLS = ("log_shadow", "log_midtone", "log_highlight")
_PRIMARY_WHEELS = ("lift", "gamma", "gain")
_REFINED = frozenset(("temp", "contrast", "offset", "saturation", "hue_shift", *_PRIMARY_WHEELS, *_LOG_CONTROLS))
_DEFAULT_PIVOT = 0.435
_LOG_LOW, _LOG_HIGH = 0.333, 0.550
_LOG_KNOT = 0.16
_LOG_BLACK_SHIFT = 32.0 / 65535.0
# Measured from the slope of the unclipped Offset RGB chart.
_OFFSET_UNIT = 961.0 / (65535.0 * 2.0)


@dataclass(frozen=True)
class _Curves:
    domain: np.ndarray
    contrast_knots: np.ndarray
    contrast_deltas: np.ndarray
    log_deltas: np.ndarray
    temperature_knots: np.ndarray
    temperature_matrices: np.ndarray


@lru_cache(maxsize=1)
def _curves() -> _Curves:
    with np.load(PROFILE_PATH, allow_pickle=False) as data:
        contrast = data["contrast_knots"]
        contrast_deltas = data["contrast_deltas"]
        log_deltas = data["log_deltas"]
        temperature = data["temperature_knots"]
        matrices = data["temperature_matrices"]
    count = 1024
    if (contrast.ndim != 1 or len(contrast) < 2 or contrast[0] != 0.0
            or not np.all(np.diff(contrast) > 0.0)
            or contrast_deltas.shape != (len(contrast), count)
            or log_deltas.shape != (3, 2, count)
            or temperature.ndim != 1 or len(temperature) < 2
            or not np.all(np.diff(temperature) > 0.0)
            or matrices.shape != (len(temperature), 3, 3)
            or not all(np.isfinite(a).all() for a in (contrast, contrast_deltas, log_deltas, temperature, matrices))):
        raise ValueError("Invalid Rec.709 primary calibration v2 curves")
    domain = np.rint(np.linspace(0.0, 65535.0, count)) / 65535.0
    return _Curves(domain, contrast, contrast_deltas, log_deltas, temperature, matrices)


@lru_cache(maxsize=2)
def _encoded_grid(size: int) -> np.ndarray:
    b, g, r = np.indices((size,) * 3, dtype=np.float32)
    return _readonly_grid(np.stack((r, g, b), axis=-1) / (size - 1))


def _interpolate_knots(value: float, knots: np.ndarray, rows: np.ndarray) -> np.ndarray:
    hi = min(max(int(np.searchsorted(knots, value)), 1), len(knots) - 1)
    lo = hi - 1
    weight = (value - knots[lo]) / (knots[hi] - knots[lo])
    return (rows[lo] + weight * (rows[hi] - rows[lo])).astype(np.float32)


def _apply_contrast(color: np.ndarray, amount: float, pivot: float, lum_mix: float) -> np.ndarray:
    if amount < 0.0:
        return color + amount * (color - pivot)
    curves = _curves()
    delta = _interpolate_knots(amount, curves.contrast_knots, curves.contrast_deltas)
    pivot = min(max(pivot, 1e-5), 1.0 - 1e-5)

    def adjust(values: np.ndarray) -> np.ndarray:
        # Keep the measured shape on each side of the selected pivot.
        scale = np.where(values < pivot, _DEFAULT_PIVOT / pivot,
                         (1.0 - _DEFAULT_PIVOT) / (1.0 - pivot)).astype(np.float32)
        mapped = _DEFAULT_PIVOT + (values - pivot) * scale
        shifted = mapped + np.interp(mapped, curves.domain, delta).astype(np.float32)
        inverse_scale = np.where(shifted < _DEFAULT_PIVOT, pivot / _DEFAULT_PIVOT,
                                 (1.0 - pivot) / (1.0 - _DEFAULT_PIVOT)).astype(np.float32)
        return pivot + (shifted - _DEFAULT_PIVOT) * inverse_scale

    rgb = adjust(color)
    if lum_mix:
        rgb += (lum_mix * (adjust(color @ _LUMA) - rgb @ _LUMA))[..., None]
    return rgb


def _wheel_delta(values: np.ndarray, amount: np.ndarray | float, wheel: str) -> np.ndarray:
    if wheel == "lift":
        return 2.0 * amount * (1.0 - values)
    if wheel == "gain":
        return amount * values
    amplitude = np.asarray(amount, dtype=np.float32)
    signed_stops = 4.0 * np.abs(amplitude)
    exponent = np.where(amplitude < 0.0, 1.0 + signed_stops, 1.0 / (1.0 + signed_stops))
    bounded = np.clip(values, 0.0, 1.0)
    return bounded**exponent - bounded


def _apply_wheel(color: np.ndarray, wheel: str, rgb: np.ndarray, master: float,
                 lum_mix: float) -> np.ndarray:
    delta = _wheel_delta(color, rgb, wheel)
    if lum_mix:
        change_y = _wheel_delta(color @ _LUMA, master, wheel) - delta @ _LUMA
        delta += (lum_mix * change_y)[..., None]
    return color + delta


def _apply_log(color: np.ndarray, amounts: tuple[tuple[float, ...], ...],
               low: float, high: float) -> np.ndarray:
    curves = _curves()
    low = min(max(low, 0.0), 1.0)
    high = min(max(high, 0.0), 1.0)
    shadow_scale = low / _LOG_LOW
    highlight_scale = (1.0 - high) / (1.0 - _LOG_HIGH)
    middle_scale = max(high - low, 0.01) / (_LOG_HIGH - _LOG_LOW)
    middle = (high + low) * 0.5
    result = color.copy()
    for channel in range(3):
        source = color[..., channel]
        target = result[..., channel]
        if amounts[0][channel] and shadow_scale:
            delta = amounts[0][channel]
            shape = curves.log_deltas[0, int(delta > 0.0)]
            scale = shadow_scale if delta > 0.0 else 1.0
            response = np.interp(source / shadow_scale, curves.domain, shape, right=0.0)
            target += response.astype(np.float32) * (abs(delta) * scale / _LOG_KNOT)
        if amounts[1][channel]:
            delta = amounts[1][channel]
            mapped = (_LOG_LOW + _LOG_HIGH) * 0.5 + (source - middle) / middle_scale
            target += np.interp(mapped, curves.domain, curves.log_deltas[1, int(delta > 0.0)],
                                left=0.0, right=0.0).astype(np.float32) * (abs(delta) * middle_scale / _LOG_KNOT)
        if amounts[2][channel] and highlight_scale:
            delta = amounts[2][channel]
            mapped = 1.0 - (1.0 - source) / highlight_scale
            scale = highlight_scale if delta < 0.0 else 1.0
            response = np.interp(mapped, curves.domain, curves.log_deltas[2, int(delta > 0.0)], left=0.0)
            target += response.astype(np.float32) * (abs(delta) * scale / _LOG_KNOT)
    # Resolve also shifts its code-domain black level slightly while Log
    # correction is active. Fade it in near neutral for a continuous gesture.
    strength = max(abs(amount) for band in amounts for amount in band)
    return result - _LOG_BLACK_SHIFT * min(strength / 0.01, 1.0)


@lru_cache(maxsize=_GRID_CACHE_SIZE)
def refined_grid(profile: PrimaryCalibration, ordered: tuple[tuple[Control, float], ...],
                 pivot: float, low: float, high: float, lum_mix: float) -> np.ndarray:
    """Build one shared, immutable lattice for CPU, OpenGL, and Metal."""
    if not all(math.isfinite(value) for value in (pivot, low, high, lum_mix)):
        raise ValueError("Primary calibration boundaries and Lum Mix must be finite")
    float32_max = float(np.finfo(np.float32).max)
    if any(abs(value) > float32_max for _, value in ordered):
        raise ValueError("Primary calibration control exceeds float32 range")
    controls = dict(ordered)
    lum_mix = min(max(lum_mix, 0.0), 100.0) / 100.0
    original = {key: value for key, value in controls.items() if key[0] not in _REFINED}
    base = profile.build_grid(original, extrapolation="linear")
    source = _encoded_grid(base.shape[0])
    color = source + base
    temperature = controls.get(("temp", "value"), 0.0)
    if temperature:
        curves = _curves()
        matrix = _interpolate_knots(temperature, curves.temperature_knots, curves.temperature_matrices)
        color = color @ matrix.T
    for wheel in _PRIMARY_WHEELS:
        rgb = np.array([controls.get((wheel, channel), 0.0) for channel in "rgb"], dtype=np.float32)
        master = controls.get((wheel, "y"), 0.0)
        if (master and lum_mix) or np.any(rgb):
            color = _apply_wheel(color, wheel, rgb, master, lum_mix)
    contrast = controls.get(("contrast", "value"), 0.0)
    if contrast:
        color = _apply_contrast(color, contrast, pivot, lum_mix)
    log = tuple(tuple(controls.get((name, channel), 0.0) for channel in "rgb") for name in _LOG_CONTROLS)
    if any(map(any, log)):
        color = _apply_log(color, log, low, high)
    color += np.array([controls.get(("offset", c), 0.0) for c in "rgb"], dtype=np.float32) * _OFFSET_UNIT
    saturation = controls.get(("saturation", "value"), 0.0)
    if saturation:
        color += (saturation / 50.0) * (color - (color @ _CHROMA_LUMA)[..., None])
    hue = controls.get(("hue_shift", "value"), 0.0)
    if hue:
        radians = hue * math.pi / 50.0
        cosine = math.cos(radians)
        color = (cosine * color + (1.0 - cosine) * (color @ _CHROMA_LUMA)[..., None]
                 + math.sin(radians) * (color @ _HUE_SINE.T))
    delta = (color - source).astype(np.float32, copy=False)
    if not np.isfinite(delta).all():
        raise ValueError("Primary calibration response exceeds float32 range")
    return _readonly_grid(delta)
