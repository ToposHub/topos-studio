"""Pointer response for the primary grading controls."""

import math

from src.features.color.core.composition_uniforms import offset_chroma_scale, offset_master_scale


PUCK_DRAG_GAIN = 0.10

# Master changes in factor/offset units per Qt logical pixel. Offset displays
# 100 times its internal bias, including in the Color Bars palette.
_MASTER_RATE = {
    "lift": 0.08 / 120.0,
    "gamma": 0.04 / 120.0,
    "gain": 0.16 / 120.0,
    "offset": 0.04 / 120.0,
    "log_offset": 0.04 / 120.0,
    "log_shadow": 0.16 / 120.0,
    "log_midtone": 0.16 / 120.0,
    "log_highlight": 0.16 / 120.0,
}

# The Y/RGB number cells use a different gain from the master dials.
_CHANNEL_DRAG_AMOUNTS = {
    "lift": 0.10,
    "gamma": 0.05,
    "gain": 0.26,
    "offset": 9.60,
    "log_offset": 9.60,
    "log_shadow": 0.22,
    "log_midtone": 0.05,
    "log_highlight": 0.22,
}

# Resolve Studio 20.2.1.6 changes these amounts over a 120 logical pixel
# drag, converted from displayed to stored Topos units.
_SCALAR_DRAG_AMOUNTS = {
    "temp": 12.0,
    "tint": 60.0,
    "contrast": 24.0,
    "pivot": 0.24,
    "mid_detail": 60.0,
    "vibrance": 60.0,
    "shadow": 60.0,
    "highlights": 60.0,
    "saturation": 48.0,
    "hue_shift": 86.4,
    "lum_mix": 24.0,
    "log_low_range": 0.24,
    "log_high_range": 0.24,
}

# At roughly 0.12 wheel radii, Resolve's Offset reads +8.15 blue for a
# rightward move and +6.92 red for an upward move. The other wheels use the
# same axes at different strengths. Green follows the Rec.709 Y constraint.
_CHROMA_STRENGTH = {
    "lift": 0.96,
    "gamma": 0.53,
    "gain": 3.20,
    "offset": 1.0,
    "log_offset": 1.0,
    "log_shadow": 2.20,
    "log_midtone": 0.51,
    "log_highlight": 2.23,
}
_RED_AXIS = 0.57
_BLUE_AXIS = 0.68
_Y_RED, _Y_GREEN, _Y_BLUE = 0.2126, 0.7152, 0.0722

RGB = tuple[float, float, float]
_MIN_FACTOR = 2.0 ** -6.0
_MAX_FACTOR = 2.0 ** 6.0


def master_drag_rate(mode: str, *, bars: bool = False) -> float:
    if bars and mode == "offset":
        return 0.08 / 60.0
    return _MASTER_RATE[mode]


def channel_drag_rate(mode: str, *, bars: bool = False) -> float:
    """Return the calibrated number cell speed in display units per pixel."""
    amount = 7.20 if bars and mode == "offset" else _CHANNEL_DRAG_AMOUNTS[mode]
    return amount / 120.0


def scalar_drag_rate(key: str) -> float:
    """Return the unmodified drag rate in stored parameter units per pixel."""
    return _SCALAR_DRAG_AMOUNTS[key] / 120.0


def wheel_luma(rgb: RGB) -> float:
    r, g, b = rgb
    return g + _Y_RED * (r - g) + _Y_BLUE * (b - g)


def wheel_chroma(mode: str, x: float, y: float) -> RGB:
    """Convert an on-screen point in wheel radii to a neutral-Y RGB delta."""
    strength = _CHROMA_STRENGTH[mode]
    r = -y * _RED_AXIS * strength
    b = x * _BLUE_AXIS * strength
    g = -(_Y_RED * r + _Y_BLUE * b) / _Y_GREEN
    return r, g, b


def wheel_position(mode: str, rgb: RGB) -> tuple[float, float]:
    """Recover the position independently from the common master level."""
    y = wheel_luma(rgb)
    strength = _CHROMA_STRENGTH[mode]
    return ((rgb[2] - y) / (_BLUE_AXIS * strength),
            (y - rgb[0]) / (_RED_AXIS * strength))


def brightness_multiplier(brightness: float) -> float:
    stops = math.copysign(brightness * brightness * 6.0, brightness)
    return 2.0 ** stops


def multiplier_brightness(multiplier: float) -> float:
    if abs(multiplier - 1.0) < 1e-14:
        return 0.0
    stops = math.log2(max(_MIN_FACTOR, min(_MAX_FACTOR, multiplier)))
    return math.copysign(math.sqrt(abs(stops) / 6.0), stops)


def constrain_chroma(rgb: RGB, target: RGB, *, additive: bool) -> RGB:
    """Move as far toward the target as HSB can represent at the same Y."""
    def margins(values: RGB) -> tuple[float, ...]:
        mean = sum(values) / 3.0
        if additive:
            limit = offset_master_scale()
            floor = mean - offset_chroma_scale()
            return mean + limit, limit - mean, *(value - floor for value in values)
        return mean - _MIN_FACTOR, _MAX_FACTOR - mean, *values

    fraction = 1.0
    for before, after in zip(margins(rgb), margins(target)):
        if after < -1e-14:
            start = max(0.0, before)
            fraction = min(fraction, start / (start - after))
    return tuple(before + (after - before) * fraction for before, after in zip(rgb, target))


def shift_master(rgb: RGB, delta: float, *, additive: bool) -> RGB:
    """Shift every channel by the same amount inside the stored HSB range."""
    r, g, b = rgb
    mean = (r + g + b) / 3.0
    if additive:
        limit = offset_master_scale()
        delta = max(-limit - mean, min(limit - mean, delta))
    else:
        lower = max(-min(rgb), _MIN_FACTOR - mean)
        clipped = max(lower, min(_MAX_FACTOR - mean, delta))
        if clipped != delta:
            master = wheel_luma(rgb)
            target = master + delta
            if _MIN_FACTOR <= target <= _MAX_FACTOR:
                # At a channel limit, carry on adjusting Y and give up only
                # the chroma amplitude the HSB representation cannot keep.
                return constrain_chroma((target, target, target),
                                        (r + delta, g + delta, b + delta),
                                        additive=False)
            if master > _MAX_FACTOR and target > _MAX_FACTOR and delta < 0.0:
                return tuple(channel * (target / master) for channel in rgb)
        delta = clipped
    return r + delta, g + delta, b + delta
