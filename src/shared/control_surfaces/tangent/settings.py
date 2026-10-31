"""User-facing Tangent control-surface selection values."""

from __future__ import annotations

from typing import Final


CONTROL_SURFACE_CONFIG_KEY: Final[str] = "tangent.control_surface"
COLOR_WHEEL_ROTATION_CONFIG_KEY: Final[str] = "tangent.color_wheel_rotation_degrees"
HUE_DIRECTION_INVERTED_CONFIG_KEY: Final[str] = "tangent.hue_direction_inverted"
DEFAULT_COLOR_WHEEL_ROTATION_DEGREES: Final[float] = 120.0
TANGENT_SENSITIVITY_MIN: Final[int] = 10
# Keep room above the new 1000% trackerball default for hardware-specific
# calibration. These are the internal percentages; the preference page uses a
# separate display scale for trackerball X/Y only.
TANGENT_SENSITIVITY_MAX: Final[int] = 4800
# The preference UI uses a friendlier scale: displayed 100% represents the
# internal value 1000% (10x the native Hub increment).
TANGENT_SENSITIVITY_DISPLAY_SCALE: Final[int] = 10
# Smoothing remains opt-in because filtering interleaved X/Y packets makes
# diagonal movement feel curved or delayed.  X/Y sensitivity itself is applied
# as a velocity curve: the saved percentage is the maximum, while slow rolls
# retain a finer gain than the configured maximum.
TRACKERBALL_SMOOTHING_CONFIG_KEY: Final[str] = "tangent.trackerball_smoothing"
DEFAULT_TRACKERBALL_SMOOTHING: Final[int] = 0
CONTROL_SURFACE_DISABLED: Final[str] = "disabled"
CONTROL_SURFACE_AUTO: Final[str] = "auto"
CONTROL_SURFACE_RIPPLE: Final[str] = "ripple"
CONTROL_SURFACE_ELEMENT: Final[str] = "element"
DEFAULT_CONTROL_SURFACE: Final[str] = CONTROL_SURFACE_AUTO

# These values are percentages.  Brightness applies its value directly.  For
# trackerball X/Y, the saved value is the maximum of a non-linear speed curve:
# slow rolls retain a finer gain and fast rolls rise smoothly to the configured
# maximum, with a 2x fast-roll overspeed ceiling.  The defaults are 1000%
# internally while the
# preference UI displays that value as 100%. Brightness remains at 100% because
# it is the independent ring control rather than two-dimensional wheel motion.
TANGENT_SENSITIVITY_CONFIG_KEYS: Final[tuple[str, ...]] = (
    "tangent.lift_hue_sensitivity",
    "tangent.lift_saturation_sensitivity",
    "tangent.lift_brightness_sensitivity",
    "tangent.gamma_hue_sensitivity",
    "tangent.gamma_saturation_sensitivity",
    "tangent.gamma_brightness_sensitivity",
    "tangent.gain_hue_sensitivity",
    "tangent.gain_saturation_sensitivity",
    "tangent.gain_brightness_sensitivity",
)

TANGENT_TRACKERBALL_SENSITIVITY_CONFIG_KEYS: Final[tuple[str, ...]] = (
    "tangent.lift_hue_sensitivity",
    "tangent.lift_saturation_sensitivity",
    "tangent.gamma_hue_sensitivity",
    "tangent.gamma_saturation_sensitivity",
    "tangent.gain_hue_sensitivity",
    "tangent.gain_saturation_sensitivity",
)

DEFAULT_TANGENT_SENSITIVITY: Final[dict[str, int]] = {
    "tangent.lift_hue_sensitivity": 1000,
    "tangent.lift_saturation_sensitivity": 1000,
    "tangent.lift_brightness_sensitivity": 100,
    "tangent.gamma_hue_sensitivity": 1000,
    "tangent.gamma_saturation_sensitivity": 1000,
    "tangent.gamma_brightness_sensitivity": 100,
    "tangent.gain_hue_sensitivity": 1000,
    "tangent.gain_saturation_sensitivity": 1000,
    "tangent.gain_brightness_sensitivity": 100,
}

SUPPORTED_CONTROL_SURFACES: Final[frozenset[str]] = frozenset(
    {
        CONTROL_SURFACE_DISABLED,
        CONTROL_SURFACE_AUTO,
        CONTROL_SURFACE_RIPPLE,
        CONTROL_SURFACE_ELEMENT,
    }
)


def tangent_sensitivity_to_display(value: object) -> float:
    """Convert an internal Tangent percentage to the preference scale."""

    try:
        return float(value) / TANGENT_SENSITIVITY_DISPLAY_SCALE
    except (TypeError, ValueError):
        return 100.0


def tangent_sensitivity_from_display(value: object) -> int:
    """Convert the preference scale back to the internal percentage."""

    try:
        return int(round(float(value) * TANGENT_SENSITIVITY_DISPLAY_SCALE))
    except (TypeError, ValueError):
        return 1000


def normalize_control_surface(value: object) -> str:
    """Return a supported surface value, falling back to auto detection."""

    candidate = value if isinstance(value, str) else ""
    return candidate if candidate in SUPPORTED_CONTROL_SURFACES else DEFAULT_CONTROL_SURFACE
