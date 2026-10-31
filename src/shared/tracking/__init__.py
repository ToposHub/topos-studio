"""Shared 2D/planar tracking algorithms (A1: extracted from the Color page).

Both the Color page (mask tracking) and the Composite page (Tracker node)
consume these modules.  Nothing here may import Qt or any feature package —
pure numpy/cv2/stdlib plus ``src.shared`` only.
"""

from .corner_pin import (
    build_corner_pin_export,
    validate_corner_pin,
    write_corner_pin_export,
)

__all__ = [
    "build_corner_pin_export",
    "validate_corner_pin",
    "write_corner_pin_export",
]
