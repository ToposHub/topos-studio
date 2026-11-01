"""Shared analytic four-edge Power Window softness, in image-width percent.

The order is left, right, top, bottom in the window's local (unrotated) axes.
A softness of zero is a hard edge. Positive values define a smoothstep band
centered on that edge, half inside and half outside the original rectangle.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np


def normalize_edge_softness(value: object) -> list[float] | None:
    """Validate optional serialized four-edge widths without inventing defaults."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("edge_softness must contain four finite values between 0 and 100")
    result: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError("edge_softness values must be numbers")
        number = float(item)
        if not math.isfinite(number) or not 0.0 <= number <= 100.0:
            raise ValueError("edge_softness values must be finite and between 0 and 100")
        result.append(number)
    return result


def rasterize_rect_edge_softness(width: int, height: int,
                                 center: Sequence[float], size: Sequence[float],
                                 rotation: float, softness: Sequence[float]) -> np.ndarray:
    """Evaluate in pixel space so rotation remains isotropic on wide frames.

    Returns a float32 alpha image; it never allocates GPU resources or mutates
    model state. Preview and CPU export consume this exact implementation.
    """
    if width <= 0 or height <= 0:
        raise ValueError("Window raster dimensions must be positive")
    edges = normalize_edge_softness(softness)
    if edges is None:
        raise ValueError("Four edge softness values are required")
    angle = math.radians(float(rotation))
    ca, sa = math.cos(angle), math.sin(angle)
    dx = (np.arange(width, dtype=np.float32) + 0.5 - float(center[0]) * width)[None, :]
    dy = (np.arange(height, dtype=np.float32) + 0.5 - float(center[1]) * height)[:, None]
    local_x = ca * dx + sa * dy
    local_y = -sa * dx + ca * dy
    half_w, half_h = float(size[0]) * width * 0.5, float(size[1]) * height * 0.5
    result = np.ones((height, width), dtype=np.float32)
    for distance, soft in zip((local_x + half_w, half_w - local_x,
                               local_y + half_h, half_h - local_y), edges):
        if soft <= 0.0:
            result *= distance >= 0.0
        else:
            t = np.clip(distance / (soft * width / 100.0) + 0.5, 0.0, 1.0)
            result *= t * t * (3.0 - 2.0 * t)
    return result
