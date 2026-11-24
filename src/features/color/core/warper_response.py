"""Rec.709 Gamma 2.4 response field built from measured Color Warper edits.

The GPU receives one RGB texture with a periodic hue axis, stacked saturation
tiles and a small number of peak levels. Pixel evaluation needs no mesh solve.
The editable mesh remains the sole project data.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np

from src.features.color.core.warper_color import _hs709_basis

# The GLSL and MSL samplers share this texture layout.
PEAK_MIN, PEAK_MAX, PEAK_LEVELS = 0.21875, 0.875, 8
HUE_INTERVALS, SAT_INTERVALS = 144, 64
PROFILE_HALF_WIDTH = HUE_INTERVALS // 3
_PROFILE_NAME = "warper_response_rec709_g24_v2.npz"
_LUMA_WEIGHTS = np.array((0.238, 0.549, 0.213), dtype=np.float32)
_COMBINE_EXPONENT = np.float32(1.8)


@lru_cache(maxsize=1)
def _measured_profiles() -> tuple[np.ndarray, np.ndarray]:
    path = Path(__file__).with_name("data") / _PROFILE_NAME
    with np.load(path, allow_pickle=False) as data:
        controls = data["controls"]
        responses = data["responses"].astype(np.float32)
    expected = (len(controls), PEAK_LEVELS, SAT_INTERVALS + 1, 2 * PROFILE_HALF_WIDTH + 1, 3)
    if (controls.shape != (len(controls), 4) or responses.shape != expected
            or not np.isfinite(controls).all() or not np.isfinite(responses).all()
            or not np.isin(controls[:, 0], (0, 1, 2)).all()
            or np.any(controls[:, 3] == 0.0)):
        raise ValueError("Invalid Rec.709 Color Warper calibration profile")
    return controls, responses


def _hue_and_chroma(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    r, g, b = np.moveaxis(rgb, -1, 0)
    peak = np.maximum(r, np.maximum(g, b))
    chroma = peak - np.minimum(r, np.minimum(g, b))
    angle = np.select((r == peak, g == peak), (g - b, b - r), default=r - g)
    origin = np.select((r == peak, g == peak), (0.0, 1.0 / 3.0), default=2.0 / 3.0)
    return (origin + angle / (6.0 * np.maximum(chroma, 1.0e-10))) % 1.0, chroma, peak


@lru_cache(maxsize=1)
def _coordinates() -> tuple[np.ndarray, np.ndarray]:
    hue = np.arange(HUE_INTERVALS, dtype=np.float32) / HUE_INTERVALS
    sat = np.linspace(0.0, 1.0, SAT_INTERVALS + 1, dtype=np.float32)[:, None, None]
    source = (1.0 - sat + sat * _hs709_basis(hue)).astype(np.float32)
    return hue, source


def _fallback_response(source: np.ndarray, offsets: np.ndarray) -> np.ndarray:
    """Extend the luma-constrained parametric response through unsampled controls."""
    from src.features.color.core.warper_color import (
        RESOLVE_HS709_SPACE, _prefilter_hs709_rings, apply_resolve_hs709_texture,
    )

    if not np.any(offsets):
        return np.zeros_like(source)
    # The analytic fallback is smooth. Sample it every other texel on the
    # default grids and fill the intervening samples periodically.
    stride = 2 if max(offsets.shape[:2]) <= 16 else 1
    source_sub = source[::stride, ::stride]
    source_lin = source_sub ** 2.4
    chromatic = offsets.copy()
    chromatic[..., 2] = 0.0
    output = source_sub
    if np.any(chromatic):
        mesh = _prefilter_hs709_rings(chromatic, local_inner_rings=True)
        adjusted = apply_resolve_hs709_texture(source_lin, mesh, RESOLVE_HS709_SPACE) ** (1.0 / 2.4)
        before = np.einsum("...c,c->...", source_sub ** 2.7, _LUMA_WEIGHTS)
        after = np.einsum("...c,c->...", adjusted ** 2.7, _LUMA_WEIGHTS)
        output = adjusted * (before / np.maximum(after, 1.0e-10))[..., None] ** (1.0 / 2.7)
    if np.any(offsets[..., 2]):
        luminance = np.zeros_like(offsets)
        luminance[..., 2] = offsets[..., 2]
        mesh = _prefilter_hs709_rings(luminance, local_inner_rings=True)
        graded = apply_resolve_hs709_texture(source_lin, mesh, RESOLVE_HS709_SPACE) ** (1.0 / 2.4)
        output = output * graded.max(axis=-1, keepdims=True)
    delta = output - source_sub
    if stride == 1:
        return delta
    fields = np.empty_like(source)
    fields[::2, ::2] = delta
    fields[::2, 1::2] = (delta + np.roll(delta, -1, axis=1)) * 0.5
    fields[1::2] = (fields[:-1:2] + fields[2::2]) * 0.5
    return fields


def _rescale_hue_response(source: np.ndarray, response: np.ndarray, amount: float) -> np.ndarray:
    """Interpolate hue on its short arc and vary peak and chroma with the same move."""
    if abs(amount - 1.0) < 1.0e-6:
        return response
    start_hue, start_chroma, start_peak = _hue_and_chroma(source)
    end_hue, end_chroma, end_peak = _hue_and_chroma(np.maximum(source + response, 0.0))
    hue = start_hue + ((end_hue - start_hue + 0.5) % 1.0 - 0.5) * amount
    chroma = start_chroma + (end_chroma - start_chroma) * amount
    peak = start_peak + (end_peak - start_peak) * amount
    return peak[..., None] - chroma[..., None] * (1.0 - _hs709_basis(hue)) - source


def build_response_texture(offsets: np.ndarray) -> np.ndarray:
    """Build a packed trilinear RGB delta field for an editable HSP mesh."""
    return _build_cached(offsets.shape, np.ascontiguousarray(offsets, dtype=np.float32).tobytes())


@lru_cache(maxsize=8)
def _build_cached(shape: tuple[int, ...], mesh: bytes) -> np.ndarray:
    offsets = np.frombuffer(mesh, dtype=np.float32).reshape(shape)
    rows, cols, _ = offsets.shape
    hue, source = _coordinates()
    output = np.zeros((PEAK_LEVELS, SAT_INTERVALS + 1, HUE_INTERVALS + 1, 3), np.float32)
    remaining = offsets.copy()
    rings = np.arange(1, rows + 1)[:, None] / rows
    remaining[..., 0] = (remaining[..., 0] + 0.5) % 1.0 - 0.5
    remaining[..., 1] = np.clip(remaining[..., 1], -rings, 1.5 - rings)
    remaining[..., 2] = np.clip(remaining[..., 2], -0.5, 0.5)
    controls, samples = _measured_profiles()
    for row, col in np.argwhere(np.any(remaining != 0.0, axis=-1)):
        center, ring = col / cols, (row + 1) / rows
        move = remaining[row, col].copy()
        matches = []
        for (axis, anchor, anchor_ring, reference), sample in zip(controls, samples):
            axis = int(axis)
            if move[axis] * reference <= 0.0:
                continue
            hue_distance = abs((center - anchor + 0.5) % 1.0 - 0.5)
            proximity = max(1.0 - hue_distance * 12.0, 0.0) * max(1.0 - abs(ring - anchor_ring) * 6.0, 0.0)
            if proximity <= 1.0e-6:
                continue
            amount = move[axis] / reference
            strength = proximity ** 2 * (3.0 - 2.0 * proximity)
            strength /= 1.0 + 4.0 * max(amount - 1.0, 0.0) ** 2
            remaining[row, col, axis] *= 1.0 - strength
            matches.append((axis, min(amount, 2.0), strength, sample))
        if not matches:
            continue

        gap = (hue - center + 0.5) % 1.0 - 0.5
        columns = np.flatnonzero(abs(gap) * cols < 2.0)
        index = np.clip(gap[columns] * cols / 6.0 * HUE_INTERVALS + PROFILE_HALF_WIDTH,
                        0.0, 2 * PROFILE_HALF_WIDTH)
        left = index.astype(np.intp)
        right = np.minimum(left + 1, 2 * PROFILE_HALF_WIDTH)
        unit_rgb = source[:, columns]
        # Compose tangential and radial motion in a shared RGB power domain.
        # A tangential edit travels farther at the destination saturation.
        base = unit_rgb ** _COMBINE_EXPONENT
        shift = np.zeros((PEAK_LEVELS, *base.shape), dtype=np.float32)
        light = np.zeros_like(shift)
        for axis, amount, strength, sample in matches:
            measured = sample[:, :, left] + (sample[:, :, right] - sample[:, :, left]) * (
                index - left).astype(np.float32)[None, None, :, None]
            if axis == 2:
                light += measured * (amount * strength)
                continue
            if axis == 0:
                measured = _rescale_hue_response(unit_rgb, measured, amount)
                strength *= 1.0 + move[1] / ring
            else:
                measured *= amount
            shift += (np.maximum(unit_rgb + measured, 0.0) ** _COMBINE_EXPONENT - base) * strength
        graded = np.where(shift == 0.0, unit_rgb, np.maximum(base + shift, 0.0) ** (1.0 / _COMBINE_EXPONENT))
        gain = np.divide(graded, unit_rgb, out=np.ones_like(graded), where=unit_rgb > 1.0e-6)
        output[:, :, columns] += graded - unit_rgb + gain * light

    output[:, :, :-1] += _fallback_response(source, remaining)
    output[:, 0] = 0.0
    output[:, :, -1] = output[:, :, 0]
    texture = output.reshape(-1, HUE_INTERVALS + 1, 3)
    texture.setflags(write=False)
    return texture


def sample_response_texture(texture: np.ndarray, hue: np.ndarray, saturation: np.ndarray,
                            peak: np.ndarray) -> np.ndarray:
    """CPU equivalent of two bilinear texture reads and a peak interpolation."""
    volume = texture.reshape(PEAK_LEVELS, SAT_INTERVALS + 1, HUE_INTERVALS + 1, 3)
    x = hue * HUE_INTERVALS
    y = np.clip(saturation, 0.0, 1.0) * SAT_INTERVALS
    z = np.clip((peak - PEAK_MIN) / (PEAK_MAX - PEAK_MIN), 0.0, 1.0) * (PEAK_LEVELS - 1)
    left, low, dark = (axis.astype(np.intp) for axis in (x, y, z))
    right = np.minimum(left + 1, HUE_INTERVALS)
    high = np.minimum(low + 1, SAT_INTERVALS)
    light = np.minimum(dark + 1, PEAK_LEVELS - 1)
    dx, dy, dz = ((a - i)[:, None] for a, i in ((x, left), (y, low), (z, dark)))

    def plane(levels: np.ndarray) -> np.ndarray:
        a = volume[levels, low, left]
        b = volume[levels, high, left]
        lower = a + (volume[levels, low, right] - a) * dx
        upper = b + (volume[levels, high, right] - b) * dx
        return lower + (upper - lower) * dy

    first, second = plane(dark), plane(light)
    return first + (second - first) * dz
