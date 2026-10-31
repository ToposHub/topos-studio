"""MT-03: ``interpolate`` — the central Remotion-style animation primitive.

``interpolate(value, input_range, output_range, easing, extrapolate_left/right)``
maps a value through a piecewise function with explicit extrapolation.  It is a
pure, deterministic function of its inputs: same arguments always yield the same
result, in preview and export, on every machine.

Supported output types: numbers, hex colours (``#rgb``/``#rrggbb``/``#rrggbbaa``)
and equal-length numeric vectors.  Unsupported input (mismatched ranges,
non-monotonic input, non-finite numbers, unparseable colours) raises
``ValueError`` rather than silently producing a wrong value — the plan requires
that unsupported cases fail explicitly (MT-03).
"""

from __future__ import annotations

import math
import re
from typing import Any, Sequence

from .easing import EasingFn


Extrapolate = str  # "extend" | "clamp" | "identity"

_EXTRAPOLATE_MODES = frozenset({"extend", "clamp", "identity"})

_HEX_COLOR = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def _validate_ranges(input_range: Sequence[float], output_range: Sequence[Any]) -> None:
    if len(input_range) != len(output_range):
        raise ValueError("input_range and output_range must have equal length")
    if len(input_range) < 2:
        raise ValueError("interpolate requires at least two points")
    for index, value in enumerate(input_range):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
            raise ValueError(f"input_range[{index}] must be finite numeric")
    for index in range(1, len(input_range)):
        if input_range[index] <= input_range[index - 1]:
            raise ValueError("input_range must be strictly increasing")


def _interpolate_segment(
    value: float, x0: float, x1: float, y0: Any, y1: Any, easing: EasingFn
) -> Any:
    if x1 == x0:
        return y0
    progress = (value - x0) / (x1 - x0)
    eased = easing(progress)
    if isinstance(y0, (int, float)) and not isinstance(y0, bool):
        return y0 + (y1 - y0) * eased  # type: ignore[no-any-return]
    if isinstance(y0, str) and isinstance(y1, str):
        return _mix_colors(y0, y1, eased)
    if isinstance(y0, (tuple, list)) and isinstance(y1, (tuple, list)):
        if len(y0) != len(y1):
            raise ValueError("vector endpoints must have equal length")
        return type(y0)(a + (b - a) * eased for a, b in zip(y0, y1))  # type: ignore[no-any-return]
    raise ValueError("interpolate supports numbers, hex colours and numeric vectors only")


def interpolate(
    value: float,
    input_range: Sequence[float],
    output_range: Sequence[Any],
    *,
    easing: EasingFn | str | None = None,
    extrapolate_left: Extrapolate = "extend",
    extrapolate_right: Extrapolate = "extend",
) -> Any:
    """Map ``value`` through a piecewise interpolated function.

    Args:
        value: the input to evaluate.
        input_range: strictly increasing control inputs.
        output_range: control outputs (number / hex colour / numeric vector).
        easing: an easing callable or a registered name (default linear).
        extrapolate_left / extrapolate_right: one of ``extend`` (continue the
            nearest segment's slope), ``clamp`` (hold the edge value) or
            ``identity`` (return ``value`` unchanged).  Defaults to ``extend``.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("interpolate value must be a finite number")
    value = float(value)
    if value != value:  # NaN
        raise ValueError("interpolate value must be finite")
    _validate_ranges(input_range, output_range)
    for mode_name, mode in (("extrapolate_left", extrapolate_left), ("extrapolate_right", extrapolate_right)):
        if mode not in _EXTRAPOLATE_MODES:
            raise ValueError(f"{mode_name} must be one of 'extend', 'clamp', 'identity', got {mode!r}")
    from .easing import resolve_easing
    ease = resolve_easing(easing)

    if value < input_range[0]:
        if extrapolate_left == "identity":
            return value
        if extrapolate_left == "clamp":
            return output_range[0]
        value_proj = value
        return _interpolate_segment(value_proj, input_range[0], input_range[1],
                                     output_range[0], output_range[1], ease)
    if value > input_range[-1]:
        if extrapolate_right == "identity":
            return value
        if extrapolate_right == "clamp":
            return output_range[-1]
        return _interpolate_segment(value, input_range[-2], input_range[-1],
                                    output_range[-2], output_range[-1], ease)

    # Find the surrounding segment.
    for index in range(1, len(input_range)):
        if value <= input_range[index]:
            return _interpolate_segment(value, input_range[index - 1], input_range[index],
                                         output_range[index - 1], output_range[index], ease)
    return output_range[-1]


def _parse_color(value: str) -> tuple[int, int, int, int]:
    if not isinstance(value, str) or not _HEX_COLOR.fullmatch(value):
        raise ValueError(f"unsupported colour: {value!r}")
    hex_part = value[1:]
    if len(hex_part) == 3:
        r, g, b = (int(ch * 2, 16) for ch in hex_part)
        return r, g, b, 255
    if len(hex_part) == 6:
        return int(hex_part[0:2], 16), int(hex_part[2:4], 16), int(hex_part[4:6], 16), 255
    return int(hex_part[0:2], 16), int(hex_part[2:4], 16), int(hex_part[4:6], 16), int(hex_part[6:8], 16)


def _mix_colors(a: str, b: str, t: float) -> str:
    ar, ag, ab, aa = _parse_color(a)
    br, bg, bb, ba = _parse_color(b)
    # Overshooting easings / "extend" extrapolation push t outside [0, 1];
    # colour channels have no out-of-range representation (CSS clamps), so
    # clamp each channel to 8-bit range — the result must always be a valid
    # hex colour that round-trips through _parse_color.
    r = min(255, max(0, round(ar + (br - ar) * t)))
    g = min(255, max(0, round(ag + (bg - ag) * t)))
    bl = min(255, max(0, round(ab + (bb - ab) * t)))
    al = min(255, max(0, round(aa + (ba - aa) * t)))
    if aa == 255 and ba == 255:
        return f"#{r:02x}{g:02x}{bl:02x}"
    return f"#{r:02x}{g:02x}{bl:02x}{al:02x}"


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """Convenience clamp used by motion presets and tests."""
    if low > high:
        raise ValueError("clamp low must not exceed high")
    return max(low, min(high, value))


__all__ = ["clamp", "interpolate"]
