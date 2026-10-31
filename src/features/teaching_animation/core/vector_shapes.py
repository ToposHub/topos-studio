"""Portable, JSON-native geometry helpers for Motion Graphic shape layers.

These helpers intentionally do not depend on Qt.  The former standalone Vector
prototype used ``QPainterPath`` objects as its document model, which cannot be
stored safely in a project or sent to the timeline renderer.  MG stores the
same useful geometry as primitive values instead, so preview, export and video
cache all consume one representation.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping


SHAPE_KINDS = frozenset({
    "rect", "rounded_rect", "ellipse", "circle", "line", "arrow",
    "triangle", "polygon", "star", "path",
})

_SAFE_PATH_DATA = re.compile(r"^[MmLlHhVvCcSsQqTtAaZz0-9 .,+\-eE]+$")


def clamp_int(value: object, default: int, minimum: int, maximum: int) -> int:
    """Return a bounded integer without accepting booleans as numbers."""
    try:
        if isinstance(value, bool):
            raise ValueError
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def clamp_float(value: object, default: float, minimum: float, maximum: float) -> float:
    """Return a finite bounded float for JSON-authored shape parameters."""
    try:
        if isinstance(value, bool):
            raise ValueError
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = default
    if not math.isfinite(number):
        number = default
    return max(minimum, min(maximum, number))


def regular_polygon_points(sides: object, width: float, height: float) -> tuple[tuple[float, float], ...]:
    """Build clockwise points centred in a shape layer's local rectangle."""
    count = clamp_int(sides, 6, 3, 64)
    radius_x = max(0.0, float(width)) / 2.0
    radius_y = max(0.0, float(height)) / 2.0
    return tuple(
        (
            radius_x + math.cos(-math.pi / 2.0 + index * 2.0 * math.pi / count) * radius_x,
            radius_y + math.sin(-math.pi / 2.0 + index * 2.0 * math.pi / count) * radius_y,
        )
        for index in range(count)
    )


def star_points(
    points: object,
    inner_ratio: object,
    width: float,
    height: float,
) -> tuple[tuple[float, float], ...]:
    """Build alternating outer/inner vertices for a regular star."""
    count = clamp_int(points, 5, 3, 64)
    ratio = clamp_float(inner_ratio, 0.45, 0.05, 0.95)
    radius_x = max(0.0, float(width)) / 2.0
    radius_y = max(0.0, float(height)) / 2.0
    vertices: list[tuple[float, float]] = []
    for index in range(count * 2):
        radius = 1.0 if index % 2 == 0 else ratio
        angle = -math.pi / 2.0 + index * math.pi / count
        vertices.append((
            radius_x + math.cos(angle) * radius_x * radius,
            radius_y + math.sin(angle) * radius_y * radius,
        ))
    return tuple(vertices)


def sanitize_path_data(value: object) -> str:
    """Return inert SVG path data, or an empty string for unsupported input.

    Escaping alone prevents attribute injection, while this allow-list prevents
    arbitrary markup and keeps the stored document format deliberately small.
    Full path syntax is rendered by SVG, not interpreted as code.
    """
    data = str(value).strip() if isinstance(value, str) else ""
    if not data or len(data) > 32_768 or not _SAFE_PATH_DATA.fullmatch(data):
        return ""
    return data


def path_points_in_scene(
    content: Mapping[str, object],
    *,
    x: float,
    y: float,
) -> tuple[tuple[float, float], ...]:
    """Read persisted local anchors as scene-space points for direct editing."""
    raw_anchors = content.get("anchors", ())
    if not isinstance(raw_anchors, (list, tuple)):
        return ()
    points: list[tuple[float, float]] = []
    for anchor in raw_anchors:
        if not isinstance(anchor, (list, tuple)) or len(anchor) != 2:
            return ()
        local_x = clamp_float(anchor[0], 0.0, -100_000.0, 100_000.0)
        local_y = clamp_float(anchor[1], 0.0, -100_000.0, 100_000.0)
        points.append((x + local_x, y + local_y))
    return tuple(points) if len(points) >= 2 else ()


def path_geometry_from_scene_points(
    points: Iterable[tuple[float, float]],
    *,
    closed: bool = False,
) -> tuple[float, float, float, float, dict[str, object]]:
    """Normalize scene-space pen points into a portable path-layer payload."""
    normalized = tuple(
        (
            clamp_float(point[0], 0.0, -100_000.0, 100_000.0),
            clamp_float(point[1], 0.0, -100_000.0, 100_000.0),
        )
        for point in points
    )
    if len(normalized) < 2:
        raise ValueError("a vector path needs at least two anchor points")
    min_x = min(point[0] for point in normalized)
    min_y = min(point[1] for point in normalized)
    local = tuple((point[0] - min_x, point[1] - min_y) for point in normalized)
    commands = [f"M {local[0][0]:.6f} {local[0][1]:.6f}"]
    commands.extend(f"L {point[0]:.6f} {point[1]:.6f}" for point in local[1:])
    if closed:
        commands.append("Z")
    return (
        min_x,
        min_y,
        max(2.0, max(point[0] for point in local)),
        max(2.0, max(point[1] for point in local)),
        {"kind": "path", "d": " ".join(commands), "anchors": [list(point) for point in local], "closed": bool(closed)},
    )


__all__ = [
    "SHAPE_KINDS",
    "clamp_float",
    "clamp_int",
    "path_geometry_from_scene_points",
    "path_points_in_scene",
    "regular_polygon_points",
    "sanitize_path_data",
    "star_points",
]
