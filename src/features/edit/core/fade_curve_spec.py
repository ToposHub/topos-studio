"""Persisted clip fade shapes shared by the timeline and renderers."""

import math


FADE_PRESETS = ("linear", "equal_power", "smooth")
_POWER_PREFIX = "power_v1:"
_MIN_POWER = 0.15
_MAX_POWER = 6.0


def fade_power(curve: object) -> float | None:
    """Read a versioned adjustable curve, or return None for other shapes."""
    if not isinstance(curve, str) or not curve.startswith(_POWER_PREFIX):
        return None
    try:
        exponent = float(curve[len(_POWER_PREFIX):])
    except ValueError:
        return None
    return exponent if _MIN_POWER <= exponent <= _MAX_POWER else None


def is_fade_curve(curve: object) -> bool:
    return curve in FADE_PRESETS or fade_power(curve) is not None


def fade_curve_from_midpoint(gain: float) -> str:
    """Encode the power curve that passes through this gain halfway in."""
    gain = max(0.5 ** _MAX_POWER, min(0.5 ** _MIN_POWER, gain))
    return f"{_POWER_PREFIX}{math.log2(gain) * -1:.5f}"
