"""Small, dependency-free helpers for the application's RGB conventions.

The composite graph stores RGB channels in scene-linear space.  Qt color
controls and hex values are display-referred sRGB values.  Keeping the
conversion here prevents the UI and render nodes from quietly adopting
different transfer functions.
"""

from __future__ import annotations


def _clamp_unit(value: float) -> float:
    """Clamp a UI/display channel to the representable 0..1 range."""
    return max(0.0, min(1.0, float(value)))


def srgb_to_linear(value: float) -> float:
    """Convert one normalized sRGB channel to scene-linear light."""
    c = _clamp_unit(value)
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def linear_to_srgb(value: float) -> float:
    """Convert one scene-linear channel to normalized sRGB display code."""
    c = _clamp_unit(value)
    if c <= 0.0031308:
        return c * 12.92
    return 1.055 * (c ** (1.0 / 2.4)) - 0.055


def linear_rgb_to_hex(r: float, g: float, b: float) -> str:
    """Convert scene-linear RGB channels to a display-referred hex color."""
    channels = (linear_to_srgb(r), linear_to_srgb(g), linear_to_srgb(b))
    encoded = [max(0, min(255, int(channel * 255.0 + 0.5)))
               for channel in channels]
    return f"#{encoded[0]:02x}{encoded[1]:02x}{encoded[2]:02x}"

