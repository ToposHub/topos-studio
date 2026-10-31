"""R1: Figma-style canvas constraints and named breakpoints.

Constraints answer one question: when the live canvas differs from the canvas
the scene was authored on (a breakpoint or a quick 16:9 / 9:16 / 1:1 preview),
where does an element go?  Each axis pins independently:

* ``left`` / ``top``    — keep the distance from the canvas origin (identity)
* ``center``            — follow the canvas centre delta
* ``right`` / ``bottom``— keep the distance from the far edge
* ``scale``             — reposition AND resize proportionally to the axis

``keep_aspect`` forces a uniform scale factor (the smaller axis ratio) whenever
either axis resolves by scaling, so logos and portraits never stretch.

Everything here is a pure function over plain data: the evaluator, the DOM
overlay and the SVG renderer all consume the same resolver, which keeps
preview, cache and export pixel-identical for a given frame.

Additive storage (schema v3 unchanged):

* element ``layout["constraints"] = {"horizontal": ..., "vertical": ...,
  "keep_aspect": bool}`` — absent means legacy absolute behaviour
* scene ``metadata["breakpoints"]`` — named device breakpoints
* scene ``metadata["responsive_base_canvas"]`` — the authored canvas size the
  element transforms were designed against; absent disables resolution
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .model import SceneTransform


HORIZONTAL_CONSTRAINTS = ("left", "center", "right", "scale")
VERTICAL_CONSTRAINTS = ("top", "center", "bottom", "scale")

CONSTRAINTS_LAYOUT_KEY = "constraints"
BREAKPOINTS_KEY = "breakpoints"
BASE_CANVAS_KEY = "responsive_base_canvas"

#: Fast preview presets offered in the inspector (name, width, height).
QUICK_PREVIEW_RATIOS: tuple[tuple[str, int, int], ...] = (
    ("16:9", 1920, 1080),
    ("9:16", 1080, 1920),
    ("1:1", 1080, 1080),
)


@dataclass(frozen=True)
class CanvasConstraints:
    """Per-axis pinning for one element against its parent canvas."""

    horizontal: str = "left"
    vertical: str = "top"
    keep_aspect: bool = False

    def __post_init__(self) -> None:
        if self.horizontal not in HORIZONTAL_CONSTRAINTS:
            raise ValueError(f"unsupported horizontal constraint: {self.horizontal}")
        if self.vertical not in VERTICAL_CONSTRAINTS:
            raise ValueError(f"unsupported vertical constraint: {self.vertical}")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "horizontal": self.horizontal,
            "vertical": self.vertical,
        }
        if self.keep_aspect:
            out["keep_aspect"] = True
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CanvasConstraints":
        horizontal = str(data.get("horizontal", "left"))
        vertical = str(data.get("vertical", "top"))
        if horizontal not in HORIZONTAL_CONSTRAINTS:
            horizontal = "left"
        if vertical not in VERTICAL_CONSTRAINTS:
            vertical = "top"
        return cls(
            horizontal=horizontal,
            vertical=vertical,
            keep_aspect=bool(data.get("keep_aspect", False)),
        )


def read_constraints(element_layout: Mapping[str, Any] | None) -> CanvasConstraints | None:
    """Return the element's declared constraints, or None for legacy elements."""
    if not isinstance(element_layout, Mapping):
        return None
    raw = element_layout.get(CONSTRAINTS_LAYOUT_KEY)
    if not isinstance(raw, Mapping):
        return None
    return CanvasConstraints.from_dict(raw)


def resolve_constraint_transform(
    base_width: float,
    base_height: float,
    target_width: float,
    target_height: float,
    transform: SceneTransform,
    constraints: CanvasConstraints,
) -> SceneTransform:
    """Map one transform from the authored canvas onto the live canvas.

    ``scale_x`` / ``scale_y`` / ``rotation`` pass through untouched: constraint
    resolution is a placement rule, animation still composes on top of it.
    """
    if base_width <= 0.0 or base_height <= 0.0:
        return transform
    sx = target_width / base_width
    sy = target_height / base_height
    # AUD-27412: keep_aspect only takes effect when at least one axis resolves
    # by scaling (same gate as before), but the uniform factor must now resize
    # BOTH axes — previously only the "scale" axis multiplied its factor, so
    # mixed modes (e.g. horizontal="scale" + vertical="top") still stretched
    # the element, contradicting the "never stretch" contract above.  The
    # non-scaling axis keeps its documented pin rule for the leading/center/
    # trailing edge of the *resized* box.
    uniform: float | None = None
    if constraints.keep_aspect and ("scale" in (constraints.horizontal, constraints.vertical)):
        uniform = min(sx, sy)
        sx = uniform
        sy = uniform

    x, width = transform.x, transform.width
    if constraints.horizontal == "scale":
        x, width = transform.x * sx, transform.width * sx
    else:
        if uniform is not None:
            width = transform.width * uniform
        delta = target_width - base_width
        if constraints.horizontal == "center":
            x = transform.x + delta / 2.0 + (transform.width - width) / 2.0
        elif constraints.horizontal == "right":
            x = transform.x + delta + (transform.width - width)
        # "left" keeps the authored distance from the origin (identity).

    y, height = transform.y, transform.height
    if constraints.vertical == "scale":
        y, height = transform.y * sy, transform.height * sy
    else:
        if uniform is not None:
            height = transform.height * uniform
        delta = target_height - base_height
        if constraints.vertical == "center":
            y = transform.y + delta / 2.0 + (transform.height - height) / 2.0
        elif constraints.vertical == "bottom":
            y = transform.y + delta + (transform.height - height)
        # "top" keeps the authored distance from the origin (identity).

    return SceneTransform(
        x=x, y=y,
        width=max(0.0, width), height=max(0.0, height),
        scale_x=transform.scale_x, scale_y=transform.scale_y,
        rotation=transform.rotation,
    )


@dataclass(frozen=True)
class Breakpoint:
    """A named device canvas (Desktop / Tablet / Mobile ...)."""

    name: str
    width: int
    height: int

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("breakpoint name cannot be empty")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("breakpoint dimensions must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "width": self.width, "height": self.height}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Breakpoint":
        return cls(
            name=str(data.get("name", "")),
            width=int(data.get("width", 0)),
            height=int(data.get("height", 0)),
        )


#: Default device breakpoints seeded into new scenes' metadata.
DEFAULT_BREAKPOINTS: tuple[Breakpoint, ...] = (
    Breakpoint("Desktop", 1920, 1080),
    Breakpoint("Tablet", 768, 1024),
    Breakpoint("Mobile", 390, 844),
)


def read_breakpoints(metadata: Mapping[str, Any] | None) -> tuple[Breakpoint, ...]:
    """Read scene breakpoints; defaults to the device trio when unset."""
    if not isinstance(metadata, Mapping):
        return DEFAULT_BREAKPOINTS
    raw = metadata.get(BREAKPOINTS_KEY)
    if not isinstance(raw, list) or not raw:
        return DEFAULT_BREAKPOINTS
    out: list[Breakpoint] = []
    for item in raw:
        if isinstance(item, Mapping):
            try:
                out.append(Breakpoint.from_dict(item))
            except ValueError:
                continue
    return tuple(out) or DEFAULT_BREAKPOINTS


def write_breakpoints_into(
    metadata: Mapping[str, Any], breakpoints: tuple[Breakpoint, ...]
) -> dict[str, Any]:
    new = dict(metadata) if isinstance(metadata, Mapping) else {}
    new[BREAKPOINTS_KEY] = [bp.to_dict() for bp in breakpoints]
    return new


def read_base_canvas(metadata: Mapping[str, Any] | None) -> tuple[float, float] | None:
    """Read the authored (design) canvas size, or None when unset."""
    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get(BASE_CANVAS_KEY)
    if not isinstance(raw, Mapping):
        return None
    try:
        width = float(raw["width"])
        height = float(raw["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0.0 or height <= 0.0:
        return None
    return width, height


def write_base_canvas_into(
    metadata: Mapping[str, Any], width: float, height: float
) -> dict[str, Any]:
    new = dict(metadata) if isinstance(metadata, Mapping) else {}
    new[BASE_CANVAS_KEY] = {"width": float(width), "height": float(height)}
    return new


__all__ = [
    "BASE_CANVAS_KEY", "BREAKPOINTS_KEY", "Breakpoint", "CanvasConstraints",
    "CONSTRAINTS_LAYOUT_KEY", "DEFAULT_BREAKPOINTS", "HORIZONTAL_CONSTRAINTS",
    "QUICK_PREVIEW_RATIOS", "VERTICAL_CONSTRAINTS", "read_base_canvas",
    "read_breakpoints", "read_constraints", "resolve_constraint_transform",
    "write_base_canvas_into", "write_breakpoints_into",
]
